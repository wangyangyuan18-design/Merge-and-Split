from pathlib import Path
import re
import shutil
import tempfile
import zipfile

from qgis.PyQt.QtCore import Qt
from qgis.PyQt.QtGui import QColor
from qgis.PyQt.QtWidgets import (
    QDialog,
    QVBoxLayout,
    QHBoxLayout,
    QLabel,
    QPushButton,
    QRadioButton,
    QButtonGroup,
    QComboBox,
    QDialogButtonBox,
    QFileDialog,
    QMessageBox,
    QProgressBar,
)
from qgis.gui import QgsMapTool, QgsRubberBand
from qgis.core import (
    QgsProject,
    QgsVectorLayer,
    QgsFeatureRequest,
    QgsGeometry,
    QgsWkbTypes,
    QgsVectorFileWriter,
    QgsCoordinateTransform,
    QgsMessageLog,
    Qgis,
)


SPLIT_VERSION = "1.1.2"


class PolygonSplitTool(QgsMapTool):
    """Draw one polygon on the map, then hand the polygon to SplitDialog."""

    def __init__(self, iface, finished_callback):
        super().__init__(iface.mapCanvas())
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.finished_callback = finished_callback
        self.points = []
        self._press_pos = None

        self.rubberBand = QgsRubberBand(
            self.canvas, QgsWkbTypes.PolygonGeometry
        )
        self.rubberBand.setColor(QColor(255, 0, 0, 80))
        self.rubberBand.setFillColor(QColor(255, 0, 0, 35))
        self.rubberBand.setWidth(2)

    def _append_point(self, point):
        # Do not use QgsPointXY.equals(); it is not available in QGIS 3.40.
        # A tiny screen-space duplicate check prevents the first click of a
        # double-click from being added twice.
        if self.points:
            last = self.points[-1]
            dx = point.x() - last.x()
            dy = point.y() - last.y()
            if abs(dx) < 1e-12 and abs(dy) < 1e-12:
                return
        self.points.append(point)
        self._refresh_band()

    def canvasPressEvent(self, event):
        if event.button() != Qt.LeftButton:
            return
        self._press_pos = event.pos()
        self._append_point(self.toMapCoordinates(event.pos()))

    def canvasDoubleClickEvent(self, event):
        if event.button() != Qt.LeftButton:
            return

        # Depending on QGIS/Qt event delivery, the double-click press may or
        # may not have reached canvasPressEvent. Add it only if it is new.
        point = self.toMapCoordinates(event.pos())
        self._append_point(point)

        if len(self.points) < 3:
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Merge and Split",
                "至少需要 3 个点才能形成多边形。"
            )
            return

        polygon = QgsGeometry.fromPolygonXY([self.points])
        if polygon.isEmpty() or not polygon.isGeosValid():
            QMessageBox.warning(
                self.iface.mainWindow(),
                "Merge and Split",
                "多边形无效或存在自相交，请重新绘制。"
            )
            return

        self._finish(polygon)

    def keyPressEvent(self, event):
        if event.key() == Qt.Key_Escape:
            self._cancel()
        else:
            super().keyPressEvent(event)

    def _refresh_band(self):
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        if not self.points:
            return
        for point in self.points:
            self.rubberBand.addPoint(point, False)
        if len(self.points) >= 3:
            self.rubberBand.closePoints(False)
        self.rubberBand.update()

    def _finish(self, polygon):
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        self.canvas.unsetMapTool(self)
        self.finished_callback(polygon)

    def _cancel(self):
        self.rubberBand.reset(QgsWkbTypes.PolygonGeometry)
        self.points = []
        self.canvas.unsetMapTool(self)


class SplitDialog(QDialog):
    """
    Split phase:
      1. Draw a polygon.
      2. Choose delete, QGZ, or vector-file export.
      3. Exact geometry intersection is used after bounding-box filtering.
    """

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.canvas = iface.mapCanvas()
        self.polygon = None
        self.tool = None
        self.setWindowTitle("Merge and Split - 拆分/区域处理 v%s" % SPLIT_VERSION)
        self.resize(560, 430)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "<b>区域拆分 / 删除</b><br>"
            "先在地图上绘制一个多边形区域，然后选择处理方式。"
        ))

        self.draw_btn = QPushButton("① 绘制区域")
        self.draw_btn.clicked.connect(self.start_drawing)
        layout.addWidget(self.draw_btn)

        self.area_label = QLabel("当前区域：未绘制")
        self.area_label.setWordWrap(True)
        layout.addWidget(self.area_label)

        self.delete_radio = QRadioButton("② 删除区域内要素")
        self.qgz_radio = QRadioButton("② 另存为 QGZ 工程")
        self.file_radio = QRadioButton("② 导出到文件夹")
        self.delete_radio.setChecked(True)

        self.action_group = QButtonGroup(self)
        for radio in (self.delete_radio, self.qgz_radio, self.file_radio):
            self.action_group.addButton(radio)
            layout.addWidget(radio)

        file_row = QHBoxLayout()
        file_row.addWidget(QLabel("文件格式："))
        self.format_combo = QComboBox()
        self.format_combo.addItem("ESRI Shapefile (*.shp)", "ESRI Shapefile")
        self.format_combo.addItem("GeoJSON (*.geojson)", "GeoJSON")
        self.format_combo.addItem("KML (*.kml)", "KML")
        self.format_combo.addItem("GML (*.gml)", "GML")
        file_row.addWidget(self.format_combo, 1)
        layout.addLayout(file_row)

        self.folder_label = QLabel("输出文件夹：默认原工程目录")
        self.folder_label.setWordWrap(True)
        layout.addWidget(self.folder_label)

        self.folder_btn = QPushButton("选择输出文件夹…")
        self.folder_btn.clicked.connect(self.choose_folder)
        layout.addWidget(self.folder_btn)

        self.action_hint = QLabel()
        self.action_hint.setWordWrap(True)
        layout.addWidget(self.action_hint)
        self._update_controls()

        self.delete_radio.toggled.connect(self._update_controls)
        self.qgz_radio.toggled.connect(self._update_controls)
        self.file_radio.toggled.connect(self._update_controls)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.button(QDialogButtonBox.Ok).setText("执行")
        buttons.accepted.connect(self.execute)
        buttons.rejected.connect(self.close_dialog)
        layout.addWidget(buttons)

    def _update_controls(self):
        file_mode = self.file_radio.isChecked()
        self.format_combo.setEnabled(file_mode)
        self.folder_btn.setEnabled(file_mode)
        if self.delete_radio.isChecked():
            self.action_hint.setText(
                "删除模式：只删除当前已开启编辑的矢量图层中的相交要素。"
            )
        elif self.qgz_radio.isChecked():
            self.action_hint.setText(
                "QGZ 模式：自动使用原工程目录和原工程名，生成 原工程名_split.qgz。"
                "QGZ 内只使用 SHP 数据，不创建 GPKG；同时在原工程目录同步生成同名 SHP 数据文件。"
            )
        else:
            self.action_hint.setText(
                "文件模式：默认输出到原工程目录，并使用 原工程名_split_图层名 作为文件名。"
                "只有存在相交要素的图层才会输出。"
            )

    def start_drawing(self):
        if self.tool is not None:
            self.tool._cancel()
        self.polygon = None
        self.area_label.setText(
            "当前区域：绘制中……左键单击增加节点，左键双击完成，Esc 取消。"
        )
        self.tool = PolygonSplitTool(self.iface, self._polygon_finished)
        self.canvas.setMapTool(self.tool)

    def _polygon_finished(self, polygon):
        self.tool = None
        self.polygon = polygon
        self.area_label.setText(
            "当前区域：已完成（%d 个顶点）" %
            (len(polygon.asPolygon()[0]) - 1 if polygon.asPolygon() else 0)
        )

    def choose_folder(self):
        folder = QFileDialog.getExistingDirectory(
            self, "选择拆分输出文件夹"
        )
        if folder:
            self.folder_label.setText("输出文件夹：%s" % folder)

    def close_dialog(self):
        if self.tool is not None:
            self.tool._cancel()
            self.tool = None
        self.close()

    def closeEvent(self, event):
        if self.tool is not None:
            self.tool._cancel()
            self.tool = None
        super().closeEvent(event)

    def _polygon_for_layer(self, layer):
        """Transform the drawn polygon from canvas CRS to the layer CRS."""
        polygon = QgsGeometry(self.polygon)
        canvas_crs = self.canvas.mapSettings().destinationCrs()
        layer_crs = layer.crs()
        if (
            not canvas_crs.isValid()
            or not layer_crs.isValid()
            or canvas_crs == layer_crs
        ):
            return polygon

        transform = QgsCoordinateTransform(
            canvas_crs, layer_crs, QgsProject.instance()
        )
        result = polygon.transform(transform)
        try:
            code = int(result)
        except Exception:
            code = None
        if code is not None and code != 0:
            raise RuntimeError(
                "区域 CRS 转换失败：%s -> %s (code=%s)"
                % (canvas_crs.authid(), layer_crs.authid(), code)
            )
        return polygon

    def _candidate_ids(self, layer):
        try:
            polygon = self._polygon_for_layer(layer)
            request = QgsFeatureRequest(polygon.boundingBox())
            ids = []
            for feature in layer.getFeatures(request):
                geom = feature.geometry()
                if geom is None or geom.isEmpty():
                    continue
                try:
                    if geom.intersects(polygon):
                        ids.append(feature.id())
                except Exception:
                    continue
            return ids
        except Exception as exc:
            self._log(
                "[SPLIT SCAN ERROR] layer=%s source=%s reason=%s"
                % (layer.name(), layer.source(), exc),
                Qgis.Warning
            )
            return []

    def _scan_matches(self):
        result = []
        vector_count = 0
        for layer in QgsProject.instance().mapLayers().values():
            if not isinstance(layer, QgsVectorLayer):
                continue
            vector_count += 1
            ids = self._candidate_ids(layer)
            if ids:
                result.append((layer, ids))
        self._log(
            "[SPLIT SCAN] vector_layers=%d matched_layers=%d matched_features=%d"
            % (vector_count, len(result), sum(len(ids) for _, ids in result))
        )
        return result

    def _confirm_delete(self, matches):
        total = sum(len(ids) for _, ids in matches)
        editable_layers = sum(
            1 for layer, _ in matches if layer.isEditable()
        )
        return QMessageBox.question(
            self,
            "确认删除",
            "将删除 %d 个相交要素，涉及 %d 个可编辑图层。\n\n"
            "删除不可逆（在未提交编辑时可由 QGIS 回滚）。\n"
            "是否继续？" % (total, editable_layers),
            QMessageBox.Yes | QMessageBox.No,
            QMessageBox.No,
        ) == QMessageBox.Yes

    def _delete(self, matches):
        total = 0
        layers = 0
        skipped = 0

        for layer, ids in matches:
            if not layer.isEditable():
                skipped += len(ids)
                continue
            if layer.deleteFeatures(ids):
                total += len(ids)
                layers += 1

        self.iface.mapCanvas().refresh()
        return total, layers, skipped

    @staticmethod
    def _safe_filename(name):
        value = re.sub(r'[<>:"/\\\\|?*]+', "_", str(name or "").strip())
        value = value.rstrip(". ")
        return value or "layer"

    def _write_layer(self, layer, ids, file_path, driver, layer_name=None,
                     first_file=True):
        selected_before = list(layer.selectedFeatureIds())
        try:
            layer.selectByIds(ids)
            options = QgsVectorFileWriter.SaveVectorOptions()
            options.driverName = driver
            options.onlySelectedFeatures = True
            options.fileEncoding = "UTF-8"
            options.layerName = layer_name or self._safe_filename(layer.name())
            if driver == "GPKG":
                options.actionOnExistingFile = (
                    QgsVectorFileWriter.CreateOrOverwriteFile
                    if first_file
                    else QgsVectorFileWriter.CreateOrOverwriteLayer
                )

            error_code, new_file, new_layer, error_message = (
                QgsVectorFileWriter.writeAsVectorFormatV3(
                    layer,
                    str(file_path),
                    QgsProject.instance().transformContext(),
                    options
                )
            )
            if error_code != QgsVectorFileWriter.NoError:
                raise RuntimeError(
                    error_message or "QgsVectorFileWriter 返回错误代码 %s"
                    % error_code
                )
            return new_file or str(file_path), new_layer or options.layerName
        finally:
            layer.selectByIds(selected_before)

    def _project_output_info(self):
        project_path = Path(QgsProject.instance().fileName())
        if project_path.name:
            return project_path.parent, self._safe_filename(project_path.stem)
        return Path.cwd(), "project"

    def _export_files(self, matches, folder, driver):
        out_dir = Path(folder)
        out_dir.mkdir(parents=True, exist_ok=True)
        _, project_stem = self._project_output_info()
        exported = 0
        total = 0
        errors = []
        used_names = set()
        extensions = {
            "ESRI Shapefile": ".shp",
            "GeoJSON": ".geojson",
            "KML": ".kml",
            "GML": ".gml",
        }
        ext = extensions[driver]
        for layer, ids in matches:
            try:
                base = self._unique_name(
                    "%s_split_%s" % (
                        project_stem, self._safe_filename(layer.name())
                    ),
                    used_names
                )
                path = out_dir / (base + ext)
                if driver == "ESRI Shapefile":
                    self._remove_shapefile_bundle(path.with_suffix(""))
                else:
                    try:
                        if path.exists():
                            path.unlink()
                    except Exception:
                        pass
                self._write_layer(
                    layer, ids, path, driver,
                    layer_name=base, first_file=True
                )
                exported += 1
                total += len(ids)
            except Exception as exc:
                errors.append("%s: %s" % (layer.name(), exc))
        return exported, total, errors

    @staticmethod
    def _remove_shapefile_bundle(stem):
        stem = Path(stem)
        for path in stem.parent.glob(stem.name + ".*"):
            try:
                path.unlink()
            except FileNotFoundError:
                pass

    @staticmethod
    def _copy_shapefile_bundle(source_stem, target_stem):
        source_stem = Path(source_stem)
        target_stem = Path(target_stem)
        target_stem.parent.mkdir(parents=True, exist_ok=True)
        SplitDialog._remove_shapefile_bundle(target_stem)
        copied = []
        for source in sorted(source_stem.parent.glob(source_stem.name + ".*")):
            target = target_stem.parent / (target_stem.name + source.suffix)
            shutil.copy2(source, target)
            copied.append(target)
        if not target_stem.with_suffix(".shp").exists():
            raise RuntimeError(
                "Shapefile 输出不完整：%s" % source_stem.name
            )
        return copied

    def _create_qgz(self, matches, output_path):
        output_path = Path(output_path)
        output_path.parent.mkdir(parents=True, exist_ok=True)
        project_dir, project_stem = self._project_output_info()
        split_folder = project_dir / ("%s_split SHP" % project_stem)
        split_folder.mkdir(parents=True, exist_ok=True)
        # This folder is owned by the current Split output. Remove only
        # files generated by this project's Split naming convention so a
        # rerun cannot leave stale SHP sidecars/layers behind.
        for stale in split_folder.glob("%s_split_*.*" % project_stem):
            try:
                stale.unlink()
            except FileNotFoundError:
                pass
        temp_dir = Path(tempfile.mkdtemp(prefix="merge_split_qgz_"))
        created_external = []
        try:
            qgs_path = temp_dir / "split_project.qgs"
            new_project = QgsProject()
            new_project.setCrs(QgsProject.instance().crs())
            new_project.setFilePathStorage(Qgis.FilePathType.Relative)
            used_names = set()
            created = []
            external_bundles = []
            total = 0

            for layer, ids in matches:
                layer_name = self._unique_name(
                    "%s_split_%s" % (
                        project_stem, self._safe_filename(layer.name())
                    ),
                    used_names
                )
                temp_shp = temp_dir / (layer_name + ".shp")
                self._write_layer(
                    layer, ids, temp_shp, "ESRI Shapefile",
                    layer_name=layer_name, first_file=True
                )
                external_stem = split_folder / layer_name
                self._copy_shapefile_bundle(temp_shp, external_stem)
                external_bundles.append(external_stem)
                created_external.append(external_stem)

                copied = QgsVectorLayer(str(temp_shp), layer.name(), "ogr")
                if not copied.isValid():
                    raise RuntimeError(
                        "无法重新打开输出 SHP 图层：%s" % layer.name()
                    )
                self._copy_layer_style(layer, copied)
                new_project.addMapLayer(copied)
                created.append(copied)
                total += len(ids)

            if not created:
                raise RuntimeError("区域内没有可输出的要素。")
            if not new_project.write(str(qgs_path)):
                raise RuntimeError(
                    "QGIS 工程写入失败：" + new_project.error()
                )
            if output_path.exists():
                output_path.unlink()
            with zipfile.ZipFile(
                output_path, "w", compression=zipfile.ZIP_DEFLATED
            ) as archive:
                for item in sorted(temp_dir.iterdir()):
                    if item.is_file():
                        archive.write(item, item.name)
            return len(created), total, external_bundles, split_folder
        except Exception:
            for stem in created_external:
                self._remove_shapefile_bundle(stem)
            try:
                if output_path.exists():
                    output_path.unlink()
            except Exception:
                pass
            raise
        finally:
            shutil.rmtree(temp_dir, ignore_errors=True)

    def _delete_after_split(self, matches):
        """Persistently delete matched features from non-editing source layers."""
        total = 0
        layers = 0
        skipped = 0
        errors = []
        for layer, ids in matches:
            started_here = False
            try:
                if layer.isEditable():
                    skipped += len(ids)
                    errors.append(
                        "%s: 图层已有未提交编辑，未自动提交" % layer.name()
                    )
                    continue
                if not layer.startEditing():
                    skipped += len(ids)
                    errors.append("%s: 无法进入编辑状态" % layer.name())
                    continue
                started_here = True
                if not layer.deleteFeatures(ids):
                    skipped += len(ids)
                    errors.append("%s: 删除要素失败" % layer.name())
                    layer.rollBack()
                    continue
                if not layer.commitChanges():
                    skipped += len(ids)
                    errors.append(
                        "%s: 提交删除失败：%s" %
                        (layer.name(), "; ".join(layer.commitErrors()))
                    )
                    layer.rollBack()
                    continue

                remaining = sum(
                    1 for fid in ids if layer.getFeature(fid).isValid()
                )
                if remaining:
                    skipped += remaining
                    errors.append(
                        "%s: 提交后仍发现 %d 个目标要素" %
                        (layer.name(), remaining)
                    )
                    continue

                total += len(ids)
                layers += 1
            except Exception as exc:
                skipped += len(ids)
                errors.append("%s: %s" % (layer.name(), exc))
                if started_here and layer.isEditable():
                    try:
                        layer.rollBack()
                    except Exception:
                        pass
        QgsProject.instance().setDirty(True)
        self.iface.mapCanvas().refresh()
        return total, layers, skipped, errors

    def _copy_layer_style(self, source, target):
        try:
            if source.renderer() is not None:
                target.setRenderer(source.renderer().clone())
        except Exception:
            pass
        try:
            if source.labeling() is not None:
                target.setLabeling(source.labeling().clone())
            target.setLabelsEnabled(source.labelsEnabled())
        except Exception:
            pass
        try:
            target.setOpacity(source.opacity())
        except Exception:
            pass

    def _log(self, message, level=Qgis.Info):
        QgsMessageLog.logMessage(
            str(message), "Merge and Split", level
        )

    def execute(self):
        if self.polygon is None or self.polygon.isEmpty():
            QMessageBox.warning(
                self, "Merge and Split", "请先绘制一个有效的区域。"
            )
            return

        matches = self._scan_matches()
        if not matches:
            QMessageBox.information(
                self,
                "Merge and Split",
                "当前工程所有矢量图层中，没有找到与该区域相交的要素。"
            )
            return

        try:
            if self.delete_radio.isChecked():
                if not self._confirm_delete(matches):
                    return
                total, layers, skipped = self._delete(matches)
                self._log(
                    "[SPLIT DELETE] layers=%d deleted=%d skipped_noneditable=%d"
                    % (layers, total, skipped)
                )
                QMessageBox.information(
                    self,
                    "Merge and Split",
                    "删除完成。\n\n"
                    "删除要素：%d\n"
                    "涉及图层：%d\n"
                    "未编辑图层跳过：%d"
                    % (total, layers, skipped)
                )
                self.polygon = None
                return

            if self.qgz_radio.isChecked():
                project_dir, project_stem = self._project_output_info()
                if not QgsProject.instance().fileName():
                    QMessageBox.warning(
                        self,
                        "Merge and Split",
                        "当前工程尚未保存。请先保存原工程，再生成原工程名_split.qgz。"
                    )
                    return

                path = project_dir / ("%s_split.qgz" % project_stem)

                editable_matches = [
                    layer.name() for layer, _ in matches if layer.isEditable()
                ]
                if editable_matches:
                    QMessageBox.warning(
                        self,
                        "无法安全拆分",
                        "以下图层存在未提交编辑：\n\n%s\n\n"
                        "请先在 QGIS 中提交/保存这些图层的编辑，再重新执行 Split。\n"
                        "插件不会自动提交这些编辑，以避免误提交其他修改。"
                        % "\n".join(editable_matches[:20])
                    )
                    self._log(
                        "[SPLIT PREFLIGHT BLOCK] editable_layers=%s" %
                        editable_matches,
                        Qgis.Warning
                    )
                    return

                self.progress.setValue(20)
                layers, total, shp_bundles, split_folder = self._create_qgz(matches, path)
                self.progress.setValue(65)
                deleted, deleted_layers, skipped, delete_errors = self._delete_after_split(matches)
                self.progress.setValue(100)
                self._log(
                    "[SPLIT QGZ] path=%s layers=%d features=%d"
                    % (path, layers, total)
                )
                message = (
                    "拆分输出完成。\n\n"
                    "删除要素：%d / %d\n"
                    "QGZ 图层：%d\n"
                    "QGZ 要素：%d\n"
                    "QGZ：%s\n"
                    "SHP 文件夹：%s"
                    % (deleted, total, layers, total, path, split_folder)
                )
                if delete_errors:
                    message += (
                        "\n\n⚠ 原工程未完全清理：%d 个要素未删除。\n%s"
                        % (skipped, "\n".join(delete_errors[:10]))
                    )
                    self._log(
                        "[SPLIT SOURCE DELETE PARTIAL] deleted=%d skipped=%d errors=%s"
                        % (deleted, skipped, delete_errors),
                        Qgis.Warning
                    )
                else:
                    message = "原工程：已成功删除拆分区域要素\n\n" + message
                QMessageBox.information(self, "Merge and Split", message)
                self.polygon = None
                return

            project_dir, _ = self._project_output_info()
            folder_text = self.folder_label.text().replace(
                "输出文件夹：", "", 1
            ).strip()
            folder = str(project_dir) if (
                not folder_text or folder_text == "默认原工程目录"
            ) else folder_text
            if not Path(folder).is_dir():
                self.choose_folder()
                folder = self.folder_label.text().replace(
                    "输出文件夹：", "", 1
                ).strip()
            if not folder or folder in ("未选择", "默认原工程目录") or not Path(folder).is_dir():
                return

            driver = self.format_combo.currentData()
            self.progress.setValue(20)
            exported, total, errors = self._export_files(
                matches, folder, driver
            )
            self.progress.setValue(100)

            self._log(
                "[SPLIT EXPORT] folder=%s driver=%s layers=%d features=%d errors=%d"
                % (folder, driver, exported, total, len(errors))
            )

            message = (
                "导出完成。\n\n"
                "格式：%s\n"
                "图层：%d\n"
                "要素：%d\n"
                "文件夹：%s"
                % (driver, exported, total, folder)
            )
            if errors:
                message += (
                    "\n\n失败图层：%d\n%s"
                    % (len(errors), "\n".join(errors[:10]))
                )
            QMessageBox.information(self, "Merge and Split", message)
            self.polygon = None

        except Exception as exc:
            self.progress.setValue(0)
            self._log(
                "[SPLIT ERROR] %s" % exc,
                Qgis.Critical
            )
            QMessageBox.critical(
                self,
                "Merge and Split",
                "拆分/区域处理失败：\n\n%s" % exc
            )

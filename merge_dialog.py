from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTreeWidget, QTreeWidgetItem,
    QPushButton, QLabel, QFileDialog, QMessageBox, QProgressBar, QCheckBox
)
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsCoordinateTransform


class MergeSplitDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.source_projects = []
        self.sources = []
        self.setWindowTitle("Merge and Split - 合并")
        self.resize(820, 560)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "<b>QGIS 文件合并</b><br>"
            "选择多个工程或单图层文件，在下方图层界面确认后，"
            "按图层名称将要素汇总到当前工程。"
        ))

        self.target_label = QLabel()
        layout.addWidget(self.target_label)
        self._refresh_target_label()

        row = QHBoxLayout()
        self.add_btn = QPushButton("添加文件…")
        self.remove_btn = QPushButton("移除选中文件")
        self.clear_btn = QPushButton("清空")
        self.add_btn.clicked.connect(self.add_files)
        self.remove_btn.clicked.connect(self.remove_selected_files)
        self.clear_btn.clicked.connect(self.clear_files)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        row.addStretch()
        layout.addLayout(row)

        self.layers = QTreeWidget()
        self.layers.setHeaderLabels(["来源文件 / 图层", "目标图层", "状态", "要素数"])
        self.layers.setColumnWidth(0, 390)
        self.layers.setColumnWidth(1, 220)
        self.layers.setSelectionMode(QTreeWidget.ExtendedSelection)
        layout.addWidget(self.layers, 1)

        self.skip_unmatched = QCheckBox("跳过目标工程中不存在的图层（推荐）")
        self.skip_unmatched.setChecked(True)
        self.skip_unmatched.stateChanged.connect(self._refresh_layer_status)
        layout.addWidget(self.skip_unmatched)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.merge_btn = QPushButton("开始合并")
        self.close_btn = QPushButton("关闭")
        self.merge_btn.clicked.connect(self.merge)
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.merge_btn)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

    def _refresh_target_label(self):
        path = QgsProject.instance().fileName()
        self.target_label.setText(
            "<b>总文件：</b>" + (path if path else "当前工程（尚未保存）")
        )

    def _target_layers(self):
        result = {}
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                result.setdefault(layer.name(), []).append(layer)
        return result

    def _load_source_layers(self, path):
        if path.lower().endswith((".qgz", ".qgs")):
            project = QgsProject()
            if not project.read(path):
                raise RuntimeError("无法读取QGIS工程：" + path)
            self.source_projects.append(project)
            return [
                layer for layer in project.mapLayers().values()
                if isinstance(layer, QgsVectorLayer)
            ]

        layer = QgsVectorLayer(path, "", "ogr")
        if not layer.isValid():
            raise RuntimeError("无法读取图层：" + path)
        return [layer]

    def _add_source_file(self, path):
        layers = self._load_source_layers(path)
        file_item = QTreeWidgetItem(self.layers)
        file_item.setText(0, path)
        file_item.setFlags(file_item.flags() | Qt.ItemIsUserCheckable)
        file_item.setCheckState(0, Qt.Checked)
        file_item.setData(0, Qt.UserRole, path)
        file_item.setExpanded(True)

        for layer in layers:
            item = QTreeWidgetItem(file_item)
            item.setText(0, layer.name())
            item.setCheckState(0, Qt.Checked)
            item.setData(0, Qt.UserRole, layer)
            item.setData(0, Qt.UserRole + 1, path)
            item.setText(3, str(layer.featureCount()))

            targets = self._target_layers().get(layer.name(), [])
            if targets:
                item.setText(1, targets[0].name())
                item.setText(2, "可合并")
                item.setToolTip(2, "目标工程存在同名图层")
            else:
                item.setText(1, "—")
                item.setText(2, "无同名目标")
                item.setToolTip(2, "当前工程没有同名图层")

            self.sources.append({
                "path": path,
                "layer": layer,
                "item": item
            })

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(
            self,
            "选择QGIS工程或单图层文件",
            "",
            "QGIS/矢量文件 (*.qgz *.qgs *.gpkg *.shp *.geojson *.json *.sqlite *.kml *.kmz);;所有文件 (*.*)"
        )
        existing = {
            self.layers.topLevelItem(i).data(0, Qt.UserRole)
            for i in range(self.layers.topLevelItemCount())
        }
        for path in paths:
            if path in existing:
                continue
            try:
                self._add_source_file(path)
            except Exception as exc:
                QMessageBox.warning(self, "读取失败", path + "\n\n" + str(exc))

        self._refresh_layer_status()

    def remove_selected_files(self):
        selected = set()
        for item in self.layers.selectedItems():
            root = item if item.parent() is None else item.parent()
            selected.add(root)
        for root in selected:
            path = root.data(0, Qt.UserRole)
            self.sources = [s for s in self.sources if s["path"] != path]
            self.layers.takeTopLevelItem(self.layers.indexOfTopLevelItem(root))
        self._refresh_layer_status()

    def clear_files(self):
        self.layers.clear()
        self.sources.clear()
        self.source_projects.clear()

    def _refresh_layer_status(self):
        targets = self._target_layers()
        for source in self.sources:
            item = source["item"]
            layer = source["layer"]
            matches = targets.get(layer.name(), [])
            if matches:
                item.setText(1, matches[0].name())
                item.setText(2, "可合并")
            else:
                item.setText(1, "—")
                item.setText(2, "跳过" if self.skip_unmatched.isChecked() else "无同名目标")

    def _compatible(self, target, source):
        if target.geometryType() != source.geometryType():
            return False
        target_names = {f.name() for f in target.fields()}
        source_names = {f.name() for f in source.fields()}
        return source_names.issubset(target_names)

    def _append_features(self, target, source):
        transform = None
        if (
            target.crs().isValid()
            and source.crs().isValid()
            and target.crs() != source.crs()
        ):
            transform = QgsCoordinateTransform(
                source.crs(), target.crs(), QgsProject.instance()
            )

        was_editing = target.isEditable()
        if not was_editing and not target.startEditing():
            raise RuntimeError("无法进入编辑状态：" + target.name())

        fields = target.fields()
        source_fields = source.fields()
        source_index = {f.name(): i for i, f in enumerate(source_fields)}
        count = 0

        for src_feat in source.getFeatures():
            feat = QgsFeature(fields)
            geom = src_feat.geometry()
            if geom and not geom.isNull():
                geom = geom.clone()
                if transform:
                    geom.transform(transform)
                feat.setGeometry(geom)

            values = []
            for field in fields:
                idx = source_index.get(field.name())
                values.append(src_feat[idx] if idx is not None else None)
            feat.setAttributes(values)

            if not target.addFeature(feat):
                raise RuntimeError("写入图层失败：" + target.name())
            count += 1

        if not was_editing:
            if not target.commitChanges():
                raise RuntimeError(
                    "提交失败：" + target.name() + "\n"
                    + "; ".join(target.commitErrors())
                )
        return count

    def _checked_sources(self):
        result = []
        for source in self.sources:
            item = source["item"]
            root = item.parent()
            if item.checkState(0) != Qt.Checked:
                continue
            if root is not None and root.checkState(0) == Qt.Unchecked:
                continue
            result.append(source)
        return result

    def merge(self):
        project = QgsProject.instance()
        if not project.mapLayers():
            QMessageBox.warning(
                self, "提示", "当前工程没有图层，无法作为总文件。"
            )
            return

        selected = self._checked_sources()
        if not selected:
            QMessageBox.warning(self, "提示", "请至少勾选一个来源图层。")
            return

        target_layers = self._target_layers()
        total_added = 0
        matched_layers = 0
        skipped_layers = []
        errors = []

        self.merge_btn.setEnabled(False)
        self.progress.setValue(0)

        try:
            for n, source_info in enumerate(selected):
                source = source_info["layer"]
                path = source_info["path"]
                self.progress.setValue(int(n * 100 / max(1, len(selected))))

                targets = target_layers.get(source.name(), [])
                if not targets:
                    skipped_layers.append(path + " :: " + source.name())
                    continue

                target = targets[0]
                if not self._compatible(target, source):
                    errors.append(
                        path + " :: " + source.name()
                        + " → 字段/几何类型不兼容"
                    )
                    continue

                try:
                    added = self._append_features(target, source)
                    total_added += added
                    matched_layers += 1
                except Exception as exc:
                    errors.append(
                        path + " :: " + source.name() + " → " + str(exc)
                    )

            project.setDirty(True)
            self.progress.setValue(100)

            message = (
                "合并完成。\n\n"
                "来源图层：%d\n"
                "匹配图层：%d\n"
                "新增要素：%d"
                % (len(selected), matched_layers, total_added)
            )
            if skipped_layers:
                message += "\n\n跳过无同名目标图层：%d" % len(skipped_layers)
            if errors:
                message += (
                    "\n\n发生错误：%d\n%s"
                    % (len(errors), "\n".join(errors[:10]))
                )

            QMessageBox.information(self, "Merge and Split", message)
            self.iface.mapCanvas().refresh()
        finally:
            self.merge_btn.setEnabled(True)

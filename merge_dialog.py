from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QFileDialog, QMessageBox, QProgressBar,
    QAbstractItemView, QRadioButton, QComboBox, QDialogButtonBox,
    QHeaderView
)
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsCoordinateTransform,
    QgsProviderRegistry
)


class MappingDialog(QDialog):
    def __init__(self, layer_name, target_names, parent=None):
        super().__init__(parent)
        self.setWindowTitle("图层处理")
        self.resize(360, 170)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>%s</b>" % layer_name))

        self.new_radio = QRadioButton("新增图层（新增）★")
        self.move_radio = QRadioButton("移至已有图层（移至）★")
        self.move_radio.setChecked(True)
        layout.addWidget(self.new_radio)
        layout.addWidget(self.move_radio)

        self.combo = QComboBox()
        self.combo.addItems(target_names)
        layout.addWidget(self.combo)

        self.new_radio.toggled.connect(self.combo.setDisabled)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def choice(self):
        if self.new_radio.isChecked():
            return "new", None
        return "move", self.combo.currentText()


class MergeSplitDialog(QDialog):
    """
    Merge phase:
      - Current QGIS project is the master/template project.
      - User selects multiple folders.
      - Each folder is scanned for vector data files.
      - Same layer names are aligned to the master layer rows.
      - Unmatched layers stay in their source-folder columns.
      - Mapping an unmatched name is global: every folder's same-name layer
        follows the same mapping.
    """

    SUPPORTED_EXTENSIONS = {
        ".shp", ".gpkg", ".geojson", ".json", ".sqlite",
        ".kml", ".kmz", ".tab", ".gml"
    }

    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.folders = []
        self.sources = []
        self.mappings = {}
        self.folder_enabled = []
        self.setWindowTitle("Merge and Split - 合并")
        self.resize(1250, 700)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)

        layout.addWidget(QLabel(
            "<b>QGIS 文件合并</b><br>"
            "以当前打开的工程为工程1（总文件），选择多个文件夹，"
            "扫描实际图层文件并按图层名称汇总。"
        ))

        self.target_label = QLabel()
        layout.addWidget(self.target_label)
        self._refresh_target_label()

        row = QHBoxLayout()
        self.add_folder_btn = QPushButton("添加文件夹…")
        self.remove_folder_btn = QPushButton("移除选中文件夹")
        self.clear_btn = QPushButton("清空")
        self.add_folder_btn.clicked.connect(self.add_folders)
        self.remove_folder_btn.clicked.connect(self.remove_selected_folder)
        self.clear_btn.clicked.connect(self.clear_folders)
        row.addWidget(self.add_folder_btn)
        row.addWidget(self.remove_folder_btn)
        row.addWidget(self.clear_btn)
        row.addStretch()
        layout.addLayout(row)

        self.layers = QTableWidget()
        self.layers.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.layers.setSelectionBehavior(QAbstractItemView.SelectItems)
        self.layers.setSelectionMode(QAbstractItemView.SingleSelection)
        self.layers.horizontalHeader().setSectionResizeMode(
            0, QHeaderView.ResizeToContents
        )
        self.layers.horizontalHeader().sectionClicked.connect(
            self._header_clicked
        )
        self.layers.cellDoubleClicked.connect(self._cell_double_clicked)
        layout.addWidget(self.layers, 1)

        layout.addWidget(QLabel(
            "提示：同名图层自动对齐；未匹配图层仍显示在其所属文件夹列。"
            "双击未匹配图层，可选择“新增”或“移至”工程1已有图层，"
            "映射会对所有文件夹中的同名图层统一生效。"
        ))

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
            "<b>工程1（总文件）：</b>" +
            (path if path else "当前工程（尚未保存）")
        )

    def _target_layers(self):
        # Follow the actual Engineering 1 layer-tree order, not the arbitrary
        # registry/mapLayers insertion order. This is also what determines the
        # matrix row order and where newly created layers are appended.
        result = {}
        root = QgsProject.instance().layerTreeRoot()
        for node in root.findLayers():
            layer = node.layer()
            if isinstance(layer, QgsVectorLayer):
                result.setdefault(layer.name(), []).append(layer)

        # Keep any valid vector layer which is not currently represented in
        # the tree as a fallback.
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                result.setdefault(layer.name(), []).append(layer)
        return result

    def _load_layer(self, path, name=None, uri=None, provider="ogr"):
        layer_path = uri if uri is not None else path
        layer = QgsVectorLayer(layer_path, name or Path(path).stem, provider)
        if not layer.isValid():
            raise RuntimeError("无法读取图层：" + path)
        return layer

    def _scan_folder(self, folder):
        result = []
        root = Path(folder)
        for path in sorted(root.iterdir()):
            if not path.is_file():
                continue
            if path.suffix.lower() not in self.SUPPORTED_EXTENSIONS:
                continue

            # GeoPackage/SQLite may contain multiple vector layers.
            if path.suffix.lower() in {".gpkg", ".sqlite"}:
                try:
                    details = QgsProviderRegistry.instance().querySublayers(
                        str(path)
                    )
                except Exception:
                    details = []

                if details:
                    for detail in details:
                        try:
                            layer = self._load_layer(
                                str(path),
                                name=detail.name(),
                                uri=detail.uri(),
                                provider=detail.providerKey()
                            )
                            result.append((layer, str(path)))
                        except Exception:
                            continue
                    continue

            try:
                layer = self._load_layer(str(path))
                result.append((layer, str(path)))
            except Exception:
                # Keep scanning the folder; one broken file must not block
                # all other usable layers.
                continue
        return result

    def _cell_text(self, layer):
        try:
            count = layer.featureCount()
            return "%s [%d]" % (layer.name(), count), count
        except Exception:
            return "%s [读取失败]" % layer.name(), -1

    def _set_layer_item(self, row, col, layer):
        text, count = self._cell_text(layer)
        if count >= 0:
            text = self._display_name(layer) + " [%d]" % count
        item = QTableWidgetItem(text)
        item.setData(Qt.UserRole, layer)
        item.setData(Qt.UserRole + 1, layer.name())
        item.setData(Qt.UserRole + 2, count)

        if count == 0:
            item.setForeground(Qt.gray)

        self.layers.setItem(row, col, item)

    def _display_name(self, layer):
        name = layer.name()
        mapping = self.mappings.get(name)
        if mapping:
            if mapping.get("action") == "new":
                return name + "（新增）★"
            if mapping.get("action") == "move":
                return name + "（移至）★"
        return name

    def _rebuild_matrix(self):
        target_layers = self._target_layers()
        target_names = list(target_layers.keys())

        # A mapped source layer belongs to the target row. For example:
        # source OLT -> existing Pre Connect Cable means the OLT cell is
        # displayed on the Pre Connect Cable row, with a global mapping mark.
        row_names = list(target_names)
        source_records_by_row = {}

        for source in self.sources:
            name = source["layer"].name()
            mapping = self.mappings.get(name)
            row_name = name

            if mapping and mapping.get("action") == "move":
                target_name = mapping.get("target")
                if target_name in target_layers:
                    row_name = target_name

            if row_name not in row_names:
                row_names.append(row_name)
            source_records_by_row.setdefault(row_name, []).append(source)

        self.layers.clear()
        self.layers.setColumnCount(1 + len(self.folders))
        self.layers.setRowCount(len(row_names))

        headers = ["工程1"]
        for i, folder in enumerate(self.folders):
            mark = "☑" if self.folder_enabled[i] else "☐"
            headers.append("%s %s" % (Path(folder).name, mark))
        self.layers.setHorizontalHeaderLabels(headers)

        name_to_row = {name: i for i, name in enumerate(row_names)}

        # Engineering 1 column remains the master order. Newly created target
        # layers are appended to this list, so they appear at the bottom.
        for name, matches in target_layers.items():
            row = name_to_row[name]
            item = QTableWidgetItem(name)
            item.setData(Qt.UserRole, matches[0])
            self.layers.setItem(row, 0, item)

        # Folder columns. A mapped source is rendered on its target row.
        seen = set()
        for source in self.sources:
            name = source["layer"].name()
            mapping = self.mappings.get(name)
            row_name = name
            if mapping and mapping.get("action") == "move":
                target_name = mapping.get("target")
                if target_name in target_layers:
                    row_name = target_name

            row = name_to_row.get(row_name)
            if row is None:
                continue
            col = self.folders.index(source["folder"]) + 1
            key = (row, col)
            if key in seen:
                continue
            seen.add(key)
            self._set_layer_item(row, col, source["layer"])

        # Make the matrix compact. Long folder names no longer consume the
        # entire window; horizontal scrolling is available when needed.
        header = self.layers.horizontalHeader()
        header.setSectionResizeMode(QHeaderView.Interactive)
        self.layers.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.layers.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)

        for col in range(self.layers.columnCount()):
            width = 90
            header_text = self.layers.horizontalHeaderItem(col)
            if header_text:
                width = max(width, min(180, len(header_text.text()) * 9 + 24))
            for row in range(self.layers.rowCount()):
                item = self.layers.item(row, col)
                if item:
                    width = max(width, min(180, len(item.text()) * 8 + 24))
            header.resizeSection(col, width)

    def add_folders(self):
        # Non-native dialog permits selecting several directories with Ctrl.
        dialog = QFileDialog(self, "选择文件夹")
        dialog.setFileMode(QFileDialog.Directory)
        dialog.setOption(QFileDialog.ShowDirsOnly, True)
        dialog.setOption(QFileDialog.DontUseNativeDialog, True)
        dialog.setWindowTitle("选择一个或多个文件夹")
        try:
            dialog.treeView().setSelectionMode(
                QAbstractItemView.ExtendedSelection
            )
        except Exception:
            pass

        if dialog.exec_() != QDialog.Accepted:
            return

        paths = dialog.selectedFiles()
        for path in paths:
            if path not in self.folders:
                try:
                    scanned = self._scan_folder(path)
                    self.folders.append(path)
                    self.folder_enabled.append(True)
                    for layer, source_path in scanned:
                        self.sources.append({
                            "folder": path,
                            "path": source_path,
                            "layer": layer
                        })
                except Exception as exc:
                    QMessageBox.warning(
                        self, "读取失败",
                        path + "\n\n" + str(exc)
                    )

        self._rebuild_matrix()

    def remove_selected_folder(self):
        col = self.layers.currentColumn()
        if col <= 0 or col > len(self.folders):
            QMessageBox.information(
                self, "提示", "请先点击要移除的文件夹列。"
            )
            return

        folder = self.folders[col - 1]
        self.folders.pop(col - 1)
        self.folder_enabled.pop(col - 1)
        self.sources = [
            source for source in self.sources
            if source["folder"] != folder
        ]
        self._rebuild_matrix()

    def clear_folders(self):
        self.folders.clear()
        self.folder_enabled.clear()
        self.sources.clear()
        self.mappings.clear()
        self.layers.clear()

    def _header_clicked(self, section):
        if section <= 0 or section > len(self.folders):
            return
        idx = section - 1
        self.folder_enabled[idx] = not self.folder_enabled[idx]
        self._rebuild_matrix()

    def _cell_double_clicked(self, row, col):
        if col <= 0 or col > len(self.folders):
            return

        item = self.layers.item(row, col)
        if item is None:
            return

        name = item.data(Qt.UserRole + 1)
        if not name:
            return

        target_layers = self._target_layers()
        if name in target_layers:
            QMessageBox.information(
                self, "提示",
                "“%s”已经与工程1同名图层自动对齐，无需设置。" % name
            )
            return

        target_names = list(target_layers.keys())
        if not target_names:
            QMessageBox.warning(
                self, "提示", "工程1目前没有可作为“移至”目标的矢量图层。"
            )
            return

        dialog = MappingDialog(name, target_names, self)
        if dialog.exec_() != QDialog.Accepted:
            return

        action, target_name = dialog.choice()
        self.mappings[name] = {
            "action": action,
            "target": target_name
        }

        if action == "new":
            try:
                source = self._first_source_by_name(name)
                # Add without implicit legend insertion, then explicitly add
                # to the root at the end. This prevents random placement.
                self._create_target_layer(source["layer"], name)
                QMessageBox.information(
                    self, "已新增",
                    "已在工程1图层最下面新增：%s（新增）★\n"
                    "所有文件夹中的同名图层将统一汇总到该图层。" % name
                )
            except Exception as exc:
                self.mappings.pop(name, None)
                QMessageBox.warning(
                    self, "新增失败", name + "\n\n" + str(exc)
                )
                return

        # For “移至”, no data is moved yet. The matrix immediately relocates
        # the source row under the selected Engineering 1 target row and marks
        # it as （移至）★. The actual feature append happens in 合并.
        self._rebuild_matrix()

    def _first_source_by_name(self, name):
        for source in self.sources:
            if source["layer"].name() == name:
                return source
        raise RuntimeError("找不到源图层：" + name)

    def _create_target_layer(self, source, name):
        target_layers = self._target_layers()
        if name in target_layers:
            return target_layers[name][0]

        geometry_map = {
            0: "Point",
            1: "LineString",
            2: "Polygon",
            3: "Unknown"
        }
        geom = geometry_map.get(source.geometryType(), "Unknown")
        uri = "%s?crs=%s" % (
            geom,
            source.crs().authid() if source.crs().isValid() else "EPSG:4326"
        )
        layer = QgsVectorLayer(uri, name, "memory")
        if not layer.isValid():
            raise RuntimeError("无法创建新图层：" + name)

        layer.dataProvider().addAttributes(list(source.fields()))
        layer.updateFields()

        project = QgsProject.instance()
        # False prevents QGIS from choosing an automatic legend position.
        project.addMapLayer(layer, False)
        root = project.layerTreeRoot()
        root.addLayer(layer)  # append to the bottom of Engineering 1
        return layer

    def _compatible(self, target, source):
        if target.geometryType() != source.geometryType():
            return False
        target_names = {f.name() for f in target.fields()}
        source_names = {f.name() for f in source.fields()}
        return source_names.issubset(target_names)

    def _resolve_target(self, source):
        name = source["layer"].name()
        targets = self._target_layers().get(name, [])
        if targets:
            return targets[0]

        mapping = self.mappings.get(name)
        if not mapping:
            return None

        if mapping["action"] == "move":
            targets = self._target_layers().get(mapping["target"], [])
            return targets[0] if targets else None

        if mapping["action"] == "new":
            return self._create_target_layer(source["layer"], name)

        return None

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
            if source["folder"] not in self.folders:
                continue
            idx = self.folders.index(source["folder"])
            if not self.folder_enabled[idx]:
                continue
            result.append(source)
        return result

    def merge(self):
        project = QgsProject.instance()
        if not project.mapLayers():
            QMessageBox.warning(
                self, "提示", "当前工程没有图层，无法作为工程1。"
            )
            return

        selected = self._checked_sources()
        if not selected:
            QMessageBox.warning(
                self, "提示", "请至少勾选一个文件夹。"
            )
            return

        total_added = 0
        matched_layers = 0
        skipped_layers = []
        errors = []

        self.merge_btn.setEnabled(False)
        self.progress.setValue(0)

        try:
            for n, source in enumerate(selected):
                self.progress.setValue(
                    int(n * 100 / max(1, len(selected)))
                )

                target = self._resolve_target(source)
                if target is None:
                    skipped_layers.append(
                        "%s :: %s" %
                        (Path(source["folder"]).name, source["layer"].name())
                    )
                    continue

                if not self._compatible(target, source):
                    errors.append(
                        "%s :: %s → 字段/几何类型不兼容" %
                        (Path(source["folder"]).name, source["layer"].name())
                    )
                    continue

                try:
                    total_added += self._append_features(
                        target, source["layer"]
                    )
                    matched_layers += 1
                except Exception as exc:
                    errors.append(
                        "%s :: %s → %s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            str(exc)
                        )
                    )

            project.setDirty(True)
            self.progress.setValue(100)

            message = (
                "合并完成。\n\n"
                "参与文件夹：%d\n"
                "参与图层：%d\n"
                "新增要素：%d"
                % (
                    sum(1 for enabled in self.folder_enabled if enabled),
                    len(selected),
                    total_added
                )
            )

            if skipped_layers:
                message += (
                    "\n\n未设置目标的图层：%d\n%s" %
                    (len(skipped_layers), "\n".join(skipped_layers[:10]))
                )

            if errors:
                message += (
                    "\n\n发生错误：%d\n%s" %
                    (len(errors), "\n".join(errors[:10]))
                )

            QMessageBox.information(self, "Merge and Split", message)
            self.iface.mapCanvas().refresh()
            self._refresh_target_label()
            self._rebuild_matrix()
        finally:
            self.merge_btn.setEnabled(True)

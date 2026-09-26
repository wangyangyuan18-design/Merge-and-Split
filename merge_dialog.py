from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QFileDialog, QMessageBox, QProgressBar,
    QAbstractItemView, QRadioButton, QComboBox, QDialogButtonBox, QStyle
)
from qgis.PyQt.QtCore import Qt
from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsCoordinateTransform,
    QgsProviderRegistry
)


class MappingDialog(QDialog):
    def __init__(self, layer_name, source_geometry_type, target_layers, parent=None):
        super().__init__(parent)
        self.setWindowTitle("图层处理")
        self.resize(360, 170)

        layout = QVBoxLayout(self)
        layout.addWidget(QLabel("<b>%s</b>" % layer_name))

        self.new_radio = QRadioButton("新增图层（新增）★")
        self.new_radio.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogNewFolder)
        )
        self.move_radio = QRadioButton("移至已有图层（移至）★")
        self.move_radio.setIcon(
            self.style().standardIcon(QStyle.SP_ArrowRight)
        )
        self.move_radio.setChecked(True)
        layout.addWidget(self.new_radio)
        layout.addWidget(self.move_radio)

        self.combo = QComboBox()
        for target in target_layers:
            if (
                hasattr(target, "wkbType")
                and self._geometry_kind(target) == source_geometry_type
            ):
                self.combo.addItem(target.name())
        layout.addWidget(self.combo)

        self.new_radio.toggled.connect(self.combo.setDisabled)

        buttons = QDialogButtonBox(
            QDialogButtonBox.Ok | QDialogButtonBox.Cancel
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @staticmethod
    def _geometry_kind(layer):
        try:
            from qgis.core import QgsWkbTypes
            return QgsWkbTypes.geometryType(layer.wkbType())
        except Exception:
            return layer.geometryType()

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
        self._feature_counts = {}
        self._source_target_map = {}
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
        self.add_folder_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogOpenButton)
        )

        self.remove_folder_btn = QPushButton("移除选中文件夹")
        self.remove_folder_btn.setIcon(
            self.style().standardIcon(QStyle.SP_TrashIcon)
        )

        self.clear_btn = QPushButton("清空")
        self.clear_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogResetButton)
        )
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
        self.merge_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogApplyButton)
        )

        self.close_btn = QPushButton("关闭")
        self.close_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogCloseButton)
        )
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
                if layer.name() not in result:
                    result[layer.name()] = [layer]
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

    def _geometry_kind(self, layer):
        """Return the normalized QGIS geometry family: Point/Line/Polygon."""
        try:
            from qgis.core import QgsWkbTypes
            return QgsWkbTypes.geometryType(layer.wkbType())
        except Exception:
            return layer.geometryType()

    def _geometry_type_name(self, layer):
        names = {
            0: "点 (Point)",
            1: "线 (LineString)",
            2: "面 (Polygon)",
            3: "未知 (Unknown)"
        }
        kind = self._geometry_kind(layer)
        return names.get(kind, str(kind))

    def _layer_source_name(self, layer, fallback=""):
        try:
            source = layer.source()
            if source:
                source_path = Path(source.split("|")[0])
                if source_path.name:
                    return source_path.name
        except Exception:
            pass
        return fallback or "工程内图层"

    def _cell_text(self, layer, source_file=None):
        try:
            count = self._feature_counts.get(layer.id())
            if count is None:
                count = layer.featureCount()
                self._feature_counts[layer.id()] = count

            name = self._display_name(layer)
            geometry = self._geometry_type_name(layer)
            source_name = self._layer_source_name(layer, source_file)
            return "%s [%d]" % (name, count), count, geometry, source_name
        except Exception:
            return "%s [读取失败]" % layer.name(), -1, "未知", "读取失败"

    def _set_layer_item(
        self, row, col, layer, geometry_mismatch=False,
        target=None, source_file=None
    ):
        text, count, geometry, source_name = self._cell_text(
            layer, source_file=source_file
        )

        # Geometry and source-file details are collapsed by default.
        tooltip = "几何：%s\n源文件：%s" % (geometry, source_name)

        item = QTableWidgetItem(text)
        if geometry_mismatch:
            item.setText(text + "  ⚠")
            tooltip = (
                "⚠ 几何类型不同\n"
                "工程1/目标：%s\n"
                "源图层：%s\n"
                "源文件：%s"
                % (
                    self._geometry_type_name(target),
                    geometry,
                    source_name
                )
            )

        item.setToolTip(tooltip)
        item.setData(Qt.UserRole, layer)
        item.setData(Qt.UserRole + 1, layer.name())
        item.setData(Qt.UserRole + 2, count)
        item.setData(Qt.UserRole + 3, geometry_mismatch)

        if geometry_mismatch:
            item.setForeground(Qt.red)
        elif count == 0:
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

        self.layers.setUpdatesEnabled(False)
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
            target_layer = matches[0]
            target_text, _, target_geometry, target_source = self._cell_text(target_layer)
            item = QTableWidgetItem(target_text)
            item.setToolTip(
                "几何：%s\n源文件：%s" % (target_geometry, target_source)
            )
            item.setData(Qt.UserRole, target_layer)
            self.layers.setItem(row, 0, item)

        # Folder columns. A mapped source is rendered on its target row.
        # Cache the exact target object represented by the matrix cell. Merge
        # uses this same object so the displayed pair and the actual pair
        # cannot diverge.
        self._source_target_map.clear()
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
            target = None
            target_matches = target_layers.get(row_name, [])
            if target_matches:
                target = self._first_target(target_matches)

            # Record the exact target shown for this source cell.
            if target is not None:
                self._source_target_map[source["layer"].id()] = target

            geometry_mismatch = (
                target is not None
                and self._geometry_kind(target) != self._geometry_kind(source["layer"])
            )
            self._set_layer_item(
                row, col, source["layer"],
                geometry_mismatch=geometry_mismatch,
                target=target,
                source_file=source.get("path")
            )

        # Keep columns compact. Avoid QHeaderView.setSectionResizeMode:
        # QGIS 3.40.14 / Qt 5.15.13 can crash during plugin startup.
        header = self.layers.horizontalHeader()
        for col in range(self.layers.columnCount()):
            width = 90
            header_text = self.layers.horizontalHeaderItem(col)
            if header_text:
                width = max(width, min(180, len(header_text.text()) * 8 + 24))
            for row in range(self.layers.rowCount()):
                item = self.layers.item(row, col)
                if item:
                    width = max(width, min(180, len(item.text()) * 7 + 24))
            self.layers.setColumnWidth(col, width)
        self.layers.setHorizontalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.layers.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        # Each layer cell now contains name / geometry / source file.
        # Give rows enough height to show the full correspondence.
        # Details stay collapsed in the main matrix.
        for row in range(self.layers.rowCount()):
            self.layers.setRowHeight(row, 28)
        self.layers.setUpdatesEnabled(True)

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
                        try:
                            self._feature_counts[layer.id()] = layer.featureCount()
                        except Exception:
                            self._feature_counts[layer.id()] = -1
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
        self._feature_counts.clear()
        self._source_target_map.clear()
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
            target = self._first_target(target_layers[name])
            source_layer = item.data(Qt.UserRole)
            if (
                isinstance(target, QgsVectorLayer)
                and isinstance(source_layer, QgsVectorLayer)
                and self._geometry_kind(target) != self._geometry_kind(source_layer)
            ):
                QMessageBox.warning(
                    self, "几何类型不兼容",
                    "“%s”与工程1同名，但几何类型不同。\n\n"
                    "工程1：%s\n源图层：%s\n\n"
                    "该图层不能直接合并，也不能通过字段映射解决。" %
                    (
                        name,
                        self._geometry_type_name(target),
                        self._geometry_type_name(source_layer)
                    )
                )
            else:
                QMessageBox.information(
                    self, "提示",
                    "“%s”已经与工程1同名图层自动对齐，无需设置。" % name
                )
            return

        source_layer = item.data(Qt.UserRole)
        source_geometry_type = (
            self._geometry_kind(source_layer)
            if isinstance(source_layer, QgsVectorLayer)
            else None
        )
        compatible_targets = []
        for targets in target_layers.values():
            for target in targets:
                if (
                    source_geometry_type is None
                    or self._geometry_kind(target) == source_geometry_type
                ):
                    compatible_targets.append(target)

        if not compatible_targets:
            QMessageBox.warning(
                self, "提示", "工程1没有与源图层点/线/面类型相同的可移至图层。"
            )
            return

        dialog = MappingDialog(
            name, source_geometry_type, compatible_targets, self
        )
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
            return self._first_target(target_layers[name])

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

    def _first_target(self, target):
        if isinstance(target, dict):
            values = []
            for items in target.values():
                if isinstance(items, list):
                    values.extend(items)
                elif isinstance(items, QgsVectorLayer):
                    values.append(items)
            return values[0] if values else None
        if isinstance(target, list):
            return target[0] if target else None
        return target

    def _geometry_debug(self, layer):
        try:
            from qgis.core import QgsWkbTypes
            wkb_name = QgsWkbTypes.displayString(layer.wkbType())
        except Exception:
            wkb_name = str(layer.wkbType())

        return "%s / WKB=%s / geometryType=%s" % (
            self._geometry_type_name(layer),
            wkb_name,
            str(self._geometry_kind(layer))
        )

    def _compatible(self, target, source):
        # Engineering 1 is the master schema. Field differences never block
        # merging; only the normalized Point/Line/Polygon geometry family does.
        target = self._first_target(target)
        if not isinstance(target, QgsVectorLayer):
            return False
        if not isinstance(source, QgsVectorLayer):
            return False
        return self._geometry_kind(target) == self._geometry_kind(source)

    def _resolve_target(self, source):
        name = source["layer"].name()
        target_layers = self._target_layers()
        targets = target_layers.get(name, [])
        if targets:
            return self._first_target(targets)

        mapping = self.mappings.get(name)
        if not mapping:
            return None

        if mapping["action"] == "move":
            targets = target_layers.get(mapping["target"], [])
            return self._first_target(targets)

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

                # Use exactly the target displayed in the main matrix.
                target = self._source_target_map.get(source["layer"].id())
                if target is None:
                    target = self._resolve_target(source)

                if target is None:
                    skipped_layers.append(
                        "%s :: %s" %
                        (Path(source["folder"]).name, source["layer"].name())
                    )
                    continue

                if not self._compatible(target, source):
                    errors.append(
                        "%s :: %s → 几何类型不兼容\n"
                        "工程1：%s\n"
                        "源图层：%s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            self._geometry_debug(target),
                            self._geometry_debug(source["layer"])
                        )
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

            # “参与图层” = 工程1与所选源文件夹共同存在的图层名称。
            # 这个数量在合并开始前已经确定，不受合并过程中的新增/映射影响。
            target_name_set = set(self._target_layers().keys())
            common_layer_names = {
                source["layer"].name()
                for source in selected
                if source["layer"].name() in target_name_set
            }

            message = (
                "合并完成。\n\n"
                "参与文件夹：%d\n"
                "参与图层：%d\n"
                "新增要素：%d"
                % (
                    sum(1 for enabled in self.folder_enabled if enabled),
                    len(common_layer_names),
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

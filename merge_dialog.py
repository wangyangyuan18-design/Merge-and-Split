from pathlib import Path

from qgis.PyQt.QtWidgets import (
    QDialog, QVBoxLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QPushButton, QLabel, QFileDialog, QMessageBox, QProgressBar,
    QAbstractItemView, QRadioButton, QComboBox, QDialogButtonBox, QStyle,
    QApplication
)
from qgis.PyQt.QtCore import Qt
PLUGIN_VERSION = "1.1.2"

from qgis.core import (
    QgsProject, QgsVectorLayer, QgsFeature, QgsGeometry, QgsCoordinateTransform,
    QgsProviderRegistry, QgsMessageLog, Qgis
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
                and self._flat_wkb_name(target) == source_geometry_type
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
            return QgsWkbTypes.geometryType(
                QgsWkbTypes.flatType(layer.wkbType())
            )
        except Exception:
            return layer.geometryType()

    @staticmethod
    def _flat_wkb_name(layer):
        try:
            from qgis.core import QgsWkbTypes
            return QgsWkbTypes.displayString(
                QgsWkbTypes.flatType(layer.wkbType())
            )
        except Exception:
            return str(layer.wkbType())

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
        self._created_target_names = set()
        self._diagnostic_lines = []
        self._last_resolution_error = ""
        self.setWindowTitle("Merge and Split - 合并 v%s" % PLUGIN_VERSION)
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

        self.copy_log_btn = QPushButton("复制诊断日志")
        self.copy_log_btn.setIcon(
            self.style().standardIcon(QStyle.SP_FileDialogDetailedView)
        )
        self.copy_log_btn.clicked.connect(self._copy_diagnostic_log)

        self.close_btn = QPushButton("关闭")
        self.close_btn.setIcon(
            self.style().standardIcon(QStyle.SP_DialogCloseButton)
        )
        self.merge_btn.clicked.connect(self.merge)
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.merge_btn)
        bottom.addWidget(self.copy_log_btn)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

    def _log(self, message, level=None):
        text_value = str(message)
        self._diagnostic_lines.append(text_value)
        try:
            QgsMessageLog.logMessage(
                text_value,
                "Merge and Split",
                level if level is not None else Qgis.Info
            )
        except Exception:
            pass

    def _target_resolution_diagnostic(self, source):
        name = self._normalized_name(source["layer"].name())
        target_layers = self._target_layers()
        candidates = target_layers.get(name, [])

        lines = [
            "[TARGET RESOLUTION]",
            "source_name=%r" % source["layer"].name(),
            "normalized_name=%r" % name,
            "source_layer_id=%s" % source["layer"].id(),
            "source_file=%s" % source.get("path", ""),
            "source_provider=%s" % source["layer"].providerType(),
            "same_name_target_count=%d" % len(candidates),
            "normalized_target_key_exists=%s" % (name in target_layers),
            "mapping=%r" % self.mappings.get(name),
        ]

        if not candidates:
            lines.append("RESULT=NO_TARGET")
        else:
            for i, target in enumerate(candidates):
                lines.append(
                    "candidate[%d]: id=%s name=%r provider=%s source=%s"
                    % (
                        i,
                        target.id(),
                        target.name(),
                        target.providerType(),
                        target.source()
                    )
                )

        return "\n".join(lines)

    def _copy_diagnostic_log(self):
        try:
            from qgis.PyQt.QtWidgets import QApplication
            QApplication.clipboard().setText(
                "\n".join(self._diagnostic_lines)
            )
            QMessageBox.information(
                self,
                "诊断日志",
                "已复制完整诊断日志。\n"
                "同时也已写入 QGIS 的 Log Messages Panel。"
            )
        except Exception as exc:
            QMessageBox.warning(self, "复制失败", str(exc))

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
                key = self._normalized_name(layer.name())
                result.setdefault(key, []).append(layer)

        # Keep any valid vector layer which is not currently represented in
        # the tree as a fallback.
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                key = self._normalized_name(layer.name())
                if key not in result:
                    result[key] = [layer]
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

    @staticmethod
    def _normalized_name(name):
        # SHP filenames determine source layer names. Ignore accidental
        # leading/trailing spaces when matching against Engineering 1.
        return str(name or "").strip()

    @staticmethod
    def _safe_int(value):
        try:
            return int(value)
        except Exception:
            return None

    @staticmethod
    def _flat_wkb_type(layer):
        """Return WKB type without Z/M dimensions."""
        from qgis.core import QgsWkbTypes
        return QgsWkbTypes.flatType(layer.wkbType())

    def _flat_wkb_name(self, layer):
        try:
            from qgis.core import QgsWkbTypes
            return QgsWkbTypes.displayString(self._flat_wkb_type(layer))
        except Exception:
            return str(self._flat_wkb_type(layer))

    def _geometry_kind(self, layer):
        """Return stable primitive geometry family: 0=Point, 1=Line, 2=Polygon."""
        try:
            from qgis.core import QgsWkbTypes
            value = QgsWkbTypes.geometryType(self._flat_wkb_type(layer))
            numeric = self._safe_int(value)
            return numeric if numeric is not None else value
        except Exception:
            value = layer.geometryType()
            numeric = self._safe_int(value)
            return numeric if numeric is not None else value

    def _is_multi_geometry(self, layer):
        try:
            from qgis.core import QgsWkbTypes
            value = QgsWkbTypes.isMultiType(self._flat_wkb_type(layer))
            return bool(value)
        except Exception:
            return False

    def _geometry_signature(self, layer):
        # Primitive tuple avoids SIP enum equality issues:
        # (geometry family, multipart flag). Z/M dimensions are ignored.
        return (
            self._safe_int(self._geometry_kind(layer)),
            bool(self._is_multi_geometry(layer))
        )

    def _geometry_type_name(self, layer):
        names = {
            0: "点 (Point)",
            1: "线 (LineString)",
            2: "面 (Polygon)",
            3: "未知 (Unknown)"
        }
        kind = self._safe_int(self._geometry_kind(layer))
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
        mapping = self.mappings.get(self._normalized_name(name))
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
        for source in self.sources:
            name = source["layer"].name()
            normalized_name = self._normalized_name(name)
            mapping = self.mappings.get(normalized_name)
            row_name = normalized_name

            if mapping and mapping.get("action") == "move":
                target_name = self._normalized_name(mapping.get("target"))
                if target_name in target_layers:
                    row_name = target_name

            if row_name not in row_names:
                row_names.append(row_name)

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
            if len(matches) > 1:
                item.setText(
                    target_text + "  ⚠ 同名目标×%d" % len(matches)
                )
                item.setToolTip(
                    "工程1存在多个同名图层，无法自动唯一对应。\n"
                    + "\n".join(
                        "%d. id=%s  source=%s" %
                        (i + 1, layer.id(), layer.source())
                        for i, layer in enumerate(matches)
                    )
                )
                item.setForeground(Qt.red)
            else:
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
            normalized_name = self._normalized_name(name)
            mapping = self.mappings.get(normalized_name)
            row_name = normalized_name
            if mapping and mapping.get("action") == "move":
                target_name = self._normalized_name(mapping.get("target"))
                if target_name in target_layers:
                    row_name = target_name

            row = name_to_row.get(row_name)
            if row is None:
                continue
            col = self.folders.index(source["folder"]) + 1
            key = (row, col)
            target = None
            target_matches = target_layers.get(row_name, [])
            if len(target_matches) == 1:
                target = target_matches[0]
                self._source_target_map[source["layer"].id()] = target

            geometry_mismatch = (
                target is not None
                and self._flat_wkb_name(target)
                != self._flat_wkb_name(source["layer"])
            )

            if key in seen:
                # Two source layers in the same folder resolve to the same
                # matrix cell. Keep the first visible cell but log the
                # ambiguity; every source still gets its own target mapping.
                existing = self.layers.item(row, col)
                if existing:
                    existing.setText(existing.text() + "  ⚠ 多源")
                    existing.setToolTip(
                        existing.toolTip()
                        + "\n⚠ 同一文件夹有多个源图层落入此单元格。"
                    )
                    existing.setForeground(Qt.red)
                continue
            seen.add(key)

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
        # QFileDialog has both a directory tree and a directory list.
        # Set ExtendedSelection on both; setting only treeView() can still
        # leave the actual directory list in single-selection mode on some
        # QGIS/Qt builds.
        for view_getter in ("treeView", "listView"):
            try:
                view = getattr(dialog, view_getter)()
                view.setSelectionMode(QAbstractItemView.ExtendedSelection)
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
        self._created_target_names.clear()
        self._diagnostic_lines.clear()
        self._last_resolution_error = ""
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

        normalized_name = self._normalized_name(name)
        target_layers = self._target_layers()
        if normalized_name in target_layers:
            candidates = target_layers[normalized_name]
            if len(candidates) > 1:
                QMessageBox.warning(
                    self,
                    "目标图层不唯一",
                    "工程1存在 %d 个同名图层“%s”，无法自动判断应该合并到哪个图层。"
                    % (len(candidates), name)
                )
                return

            target = candidates[0]
            source_layer = item.data(Qt.UserRole)
            if (
                isinstance(target, QgsVectorLayer)
                and isinstance(source_layer, QgsVectorLayer)
                and self._flat_wkb_name(target)
                != self._flat_wkb_name(source_layer)
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
            self._flat_wkb_name(source_layer)
            if isinstance(source_layer, QgsVectorLayer)
            else None
        )
        compatible_targets = []
        for targets in target_layers.values():
            # A target name must be unique in Engineering 1 to be a valid
            # destination for a global mapping.
            if len(targets) != 1:
                continue
            target = targets[0]
            if (
                source_geometry_type is None
                or self._flat_wkb_name(target) == source_geometry_type
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
                self._created_target_names.add(
                    self._normalized_name(name)
                )
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
        normalized = self._normalized_name(name)
        for source in self.sources:
            if self._normalized_name(source["layer"].name()) == normalized:
                return source
        raise RuntimeError("找不到源图层：" + name)

    def _create_target_layer(self, source, name):
        target_layers = self._target_layers()
        if name in target_layers:
            return self._first_target(target_layers[name])

        try:
            from qgis.core import QgsWkbTypes
            geom = QgsWkbTypes.displayString(source.wkbType())
        except Exception:
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
        from qgis.core import QgsWkbTypes

        try:
            wkb_raw = self._safe_int(layer.wkbType())
        except Exception:
            wkb_raw = None

        try:
            geometry_raw = self._safe_int(layer.geometryType())
        except Exception:
            geometry_raw = None

        try:
            wkb_name = QgsWkbTypes.displayString(layer.wkbType())
        except Exception:
            wkb_name = str(layer.wkbType())

        flat_wkb = self._flat_wkb_type(layer)
        try:
            flat_raw = self._safe_int(flat_wkb)
        except Exception:
            flat_raw = None

        try:
            flat_name = QgsWkbTypes.displayString(flat_wkb)
        except Exception:
            flat_name = str(flat_wkb)

        return {
            "class": type(layer).__name__,
            "id": layer.id(),
            "name": layer.name(),
            "name_repr": repr(layer.name()),
            "provider": layer.providerType(),
            "valid": layer.isValid(),
            "source": layer.source(),
            "crs": layer.crs().authid() if layer.crs().isValid() else "",
            "geometryType_raw": geometry_raw,
            "geometryType": str(self._geometry_type_name(layer)),
            "wkb_raw": wkb_raw,
            "wkb": wkb_name,
            "flatWkb_raw": flat_raw,
            "flatWkb": flat_name,
            "multi": self._is_multi_geometry(layer),
            "signature": self._geometry_signature(layer)
        }

    def _geometry_compare(self, target, source):
        target_info = self._geometry_debug(target)
        source_info = self._geometry_debug(source)

        result = {
            "target_valid": target_info["valid"],
            "source_valid": source_info["valid"],
            "family_equal": (
                target_info["signature"][0]
                == source_info["signature"][0]
            ),
            "multipart_equal": (
                target_info["signature"][1]
                == source_info["signature"][1]
            ),
            "flat_wkb_equal": (
                target_info["flatWkb_raw"]
                == source_info["flatWkb_raw"]
            ),
            "flat_wkb_name_equal": (
                target_info["flatWkb"]
                == source_info["flatWkb"]
            )
        }
        # Geometry compatibility only compares geometry signature.
        # Layer validity is reported separately as an invalid-layer error.
        # Exact Flat WKB is the geometry identity.
        # Z/M dimensions are deliberately ignored here.
        result["compatible"] = result["flat_wkb_name_equal"]
        if not result["target_valid"] or not result["source_valid"]:
            result["root_cause"] = "INVALID_LAYER"
        elif result["flat_wkb_name_equal"]:
            result["root_cause"] = "PASS"
        else:
            result["root_cause"] = "FLAT_WKB_MISMATCH"

        result["target"] = target_info
        result["source"] = source_info
        return result

    def _format_geometry_diagnostic(self, compare):
        t = compare["target"]
        s = compare["source"]

        return (
            "判定链：\\n"
            "工程1目标：%s\\n"
            "  id=%s\\n"
            "  name=%s\\n"
            "  source=%s\\n"
            "  provider=%s\\n"
            "  CRS=%s\\n"
            "  geometryType=%s (raw=%s)\\n"
            "  WKB=%s (raw=%s)\\n"
            "  flatWKB=%s (raw=%s)\\n"
            "  multi=%s\\n"
            "  signature=%s\\n\\n"
            "源图层：%s\\n"
            "  id=%s\\n"
            "  name=%s\\n"
            "  source=%s\\n"
            "  provider=%s\\n"
            "  CRS=%s\\n"
            "  geometryType=%s (raw=%s)\\n"
            "  WKB=%s (raw=%s)\\n"
            "  flatWKB=%s (raw=%s)\\n"
            "  multi=%s\\n"
            "  signature=%s\\n\\n"
            "逐项比较：\\n"
            "  目标有效：%s\\n"
            "  源有效：%s\\n"
            "  几何大类相同：%s\\n"
            "  单/多部件相同：%s\\n"
            "  flatWKB 数值相同：%s\\n"
            "  flatWKB 名称相同：%s\\n"
            "根因代码：%s\\n"
            "最终几何兼容：%s\\n"
            "目标图层有效：%s\\n"
            "源图层有效：%s\\n"
            "最终可执行合并：%s"
            % (
                t["name"], t["id"], repr(t["name"]), t["source"],
                t["provider"], t["crs"], t["geometryType"],
                t["geometryType_raw"], t["wkb"], t["wkb_raw"],
                t["flatWkb"], t["flatWkb_raw"], t["multi"],
                t["signature"],
                s["name"], s["id"], repr(s["name"]), s["source"],
                s["provider"], s["crs"], s["geometryType"],
                s["geometryType_raw"], s["wkb"], s["wkb_raw"],
                s["flatWkb"], s["flatWkb_raw"], s["multi"],
                s["signature"],
                compare["target_valid"], compare["source_valid"],
                compare["family_equal"], compare["multipart_equal"],
                compare["flat_wkb_equal"],
                compare["flat_wkb_name_equal"],
                compare["root_cause"],
                compare["compatible"],
                compare["target_valid"],
                compare["source_valid"],
                (
                    compare["compatible"]
                    and compare["target_valid"]
                    and compare["source_valid"]
                )
            )
        )

    def _compatible(self, target, source):
        target = self._first_target(target)
        if not isinstance(target, QgsVectorLayer):
            return False
        if not isinstance(source, QgsVectorLayer):
            return False
        return self._geometry_compare(target, source)["compatible"]

    def _resolve_target(self, source):
        self._last_resolution_error = ""
        name = self._normalized_name(source["layer"].name())
        target_layers = self._target_layers()
        targets = target_layers.get(name, [])
        if len(targets) == 1:
            return targets[0]
        if len(targets) > 1:
            self._last_resolution_error = "MULTIPLE_TARGETS"
            return None

        mapping = self.mappings.get(name)
        if not mapping:
            return None

        if mapping["action"] == "move":
            targets = target_layers.get(
                self._normalized_name(mapping["target"]), []
            )
            if len(targets) == 1:
                return targets[0]
            if len(targets) > 1:
                self._last_resolution_error = "MULTIPLE_TARGETS"
            else:
                self._last_resolution_error = "MAPPED_TARGET_NOT_FOUND"
            return None

        if mapping["action"] == "new":
            return self._create_target_layer(source["layer"], name)

        return None

    def _field_summary(self, layer):
        fields = []
        for field in layer.fields():
            fields.append({
                "name": field.name(),
                "type": field.typeName(),
                "length": field.length(),
                "precision": field.precision()
            })
        return fields

    def _field_mapping_diagnostic(self, target, source):
        target_fields = self._field_summary(target)
        source_fields = self._field_summary(source)
        target_names = {item["name"] for item in target_fields}
        source_names = {item["name"] for item in source_fields}

        target_by_name = {item["name"]: item for item in target_fields}
        source_by_name = {item["name"]: item for item in source_fields}
        type_conflicts = []
        for name in sorted(target_names & source_names):
            if (
                target_by_name[name]["type"] != source_by_name[name]["type"]
                or target_by_name[name]["length"]
                != source_by_name[name]["length"]
                or target_by_name[name]["precision"]
                != source_by_name[name]["precision"]
            ):
                type_conflicts.append({
                    "name": name,
                    "target": target_by_name[name],
                    "source": source_by_name[name]
                })

        return {
            "target_fields": target_fields,
            "source_fields": source_fields,
            "common": sorted(target_names & source_names),
            "target_only": sorted(target_names - source_names),
            "source_only": sorted(source_names - target_names),
            "type_conflicts": type_conflicts
        }

    @staticmethod
    def _coerce_field_value(value, target_field):
        """
        Map a source value to the Engineering 1 target field type.
        Schema differences never reject the layer itself:
          - target-only field -> NULL
          - source-only field -> ignored
          - same-name field -> best-effort type conversion
        A value which cannot be represented by the target numeric/boolean
        type becomes NULL instead of aborting the whole layer merge.
        """
        if value is None:
            return value, False, False

        type_name = str(target_field.typeName() or "").strip().lower()

        if any(token in type_name for token in ("integer64", "bigint", "integer", "int")):
            try:
                if isinstance(value, bool):
                    return int(value), True, False
                if isinstance(value, int):
                    return value, False, False
                if isinstance(value, float):
                    return int(value), True, False
                return int(str(value).strip()), True, False
            except Exception:
                return None, True, True

        if any(token in type_name for token in ("double", "real", "float", "numeric", "decimal")):
            try:
                if isinstance(value, bool):
                    return float(int(value)), True, False
                if isinstance(value, (int, float)):
                    return float(value), not isinstance(value, float), False
                return float(str(value).strip()), True, False
            except Exception:
                return None, True, True

        if "bool" in type_name:
            if isinstance(value, bool):
                return value, False, False
            if isinstance(value, (int, float)):
                return bool(value), True, False
            normalized = str(value).strip().lower()
            if normalized in {"1", "true", "t", "yes", "y"}:
                return True, True, False
            if normalized in {"0", "false", "f", "no", "n"}:
                return False, True, False
            return None, True, True

        if any(token in type_name for token in ("string", "text", "varchar", "char")):
            if isinstance(value, str):
                return value, False, False
            try:
                return str(value), True, False
            except Exception:
                return None, True, True

        # Provider-native date/time/blob/custom values remain intact.
        return value, False, False

    def _prepare_geometry_for_target(self, geom, target, source):
        if geom is None or geom.isNull():
            return geom, False

        from qgis.core import QgsWkbTypes

        target_type = target.wkbType()
        source_type = source.wkbType()
        target_z = QgsWkbTypes.hasZ(target_type)
        target_m = QgsWkbTypes.hasM(target_type)
        source_z = QgsWkbTypes.hasZ(source_type)
        source_m = QgsWkbTypes.hasM(source_type)

        changed = False
        abstract = geom.get()
        if abstract is not None:
            if source_z and not target_z:
                changed = abstract.dropZValue() or changed
            elif target_z and not source_z:
                changed = abstract.addZValue(0.0) or changed

            if source_m and not target_m:
                changed = abstract.dropMValue() or changed
            elif target_m and not source_m:
                changed = abstract.addMValue(0.0) or changed

        return geom, changed

    def _append_features(self, target, source, progress_callback=None):
        """Append features in batches while keeping QGIS responsive."""
        phase = "START_EDIT"
        current_feature_id = None
        transform = None

        if (
            target.crs().isValid()
            and source.crs().isValid()
            and target.crs() != source.crs()
        ):
            transform = QgsCoordinateTransform(
                source.crs(), target.crs(), QgsProject.instance()
            )

        fields = target.fields()
        source_fields = source.fields()
        source_index = {f.name(): i for i, f in enumerate(source_fields)}
        was_editing = target.isEditable()
        started_editing = False
        batch = []
        batch_size = 500
        count = 0
        processed = 0
        conversion_log_count = 0

        try:
            if not was_editing:
                if not target.startEditing():
                    raise RuntimeError("无法进入编辑状态：" + target.name())
                started_editing = True

            total_source = max(0, int(source.featureCount()))

            for src_feat in source.getFeatures():
                current_feature_id = src_feat.id()
                feat = QgsFeature(fields)

                geom = src_feat.geometry()
                if geom and not geom.isNull():
                    phase = "GEOMETRY_COPY"
                    geom = QgsGeometry(src_feat.geometry())

                    if transform:
                        phase = "CRS_TRANSFORM"
                        result = geom.transform(transform)
                        result_code = self._safe_int(result)
                        if result_code is not None and result_code != 0:
                            raise RuntimeError(
                                "几何 CRS 转换失败，feature=%s，result=%s"
                                % (src_feat.id(), result)
                            )

                    phase = "GEOMETRY_DIMENSION"
                    geom, dimension_changed = self._prepare_geometry_for_target(
                        geom, target, source
                    )
                    if dimension_changed and conversion_log_count < 10:
                        self._log(
                            "[GEOMETRY DIMENSION CONVERT] %s feature=%s | %s -> %s"
                            % (
                                source.name(), src_feat.id(),
                                self._geometry_debug(source)["wkb"],
                                self._geometry_debug(target)["wkb"]
                            )
                        )
                        conversion_log_count += 1
                    feat.setGeometry(geom)

                phase = "ATTRIBUTE_MAPPING"
                values = []
                for field in fields:
                    idx = source_index.get(field.name())
                    if idx is None:
                        values.append(None)
                        continue
                    raw_value = src_feat[idx]
                    converted, changed, failed = self._coerce_field_value(
                        raw_value, field
                    )
                    if changed and conversion_log_count < 10:
                        self._log(
                            "[ATTRIBUTE CONVERT] %s feature=%s field=%r "
                            "source_value=%r -> %r; target_type=%s%s"
                            % (
                                source.name(), src_feat.id(), field.name(),
                                raw_value, converted, field.typeName(),
                                " [UNCONVERTIBLE->NULL]" if failed else ""
                            ),
                            Qgis.Warning if failed else Qgis.Info
                        )
                        conversion_log_count += 1
                    values.append(converted)

                feat.setAttributes(values)
                batch.append(feat)
                processed += 1

                if len(batch) >= batch_size:
                    phase = "ADD_FEATURE_BATCH"
                    if not target.addFeatures(batch):
                        provider_error = ""
                        try:
                            provider_error = str(target.dataProvider().lastError())
                        except Exception:
                            pass
                        raise RuntimeError(
                            "批量写入失败，最后 feature=%s，target=%s%s"
                            % (
                                current_feature_id, target.name(),
                                ("；provider=" + provider_error)
                                if provider_error else ""
                            )
                        )
                    count += len(batch)
                    batch.clear()

                    if progress_callback:
                        progress_callback(processed, total_source)
                    QApplication.processEvents()

            if batch:
                phase = "ADD_FEATURE_BATCH"
                if not target.addFeatures(batch):
                    provider_error = ""
                    try:
                        provider_error = str(target.dataProvider().lastError())
                    except Exception:
                        pass
                    raise RuntimeError(
                        "批量写入失败，最后 feature=%s，target=%s%s"
                        % (
                            current_feature_id, target.name(),
                            ("；provider=" + provider_error)
                            if provider_error else ""
                        )
                    )
                count += len(batch)
                batch.clear()

            if progress_callback:
                progress_callback(processed, total_source)
            QApplication.processEvents()

            if started_editing:
                phase = "COMMIT"
                if not target.commitChanges():
                    commit_errors = "; ".join(target.commitErrors())
                    raise RuntimeError(
                        "提交失败：" + target.name()
                        + ("；" + commit_errors if commit_errors else "")
                    )
            return count

        except Exception as exc:
            if started_editing and target.isEditable():
                target.rollBack()
            raise RuntimeError(
                "失败阶段=%s；feature=%s；target=%s；source=%s；原因=%s"
                % (
                    phase, current_feature_id, target.name(),
                    source.name(), str(exc)
                )
            ) from exc


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
        resolved_count = 0
        geometry_pass_count = 0
        total_work = max(
            1,
            sum(max(0, int(source["layer"].featureCount()))
                for source in selected)
        )
        completed_work = 0
        geometry_fail_count = 0
        invalid_count = 0
        write_fail_count = 0
        merge_pass_count = 0
        skipped_layers = []
        errors = []

        self.merge_btn.setEnabled(False)
        self.progress.setValue(0)
        self._log(
            "[MERGE START] sources=%d total_features=%d"
            % (len(selected), total_work)
        )
        QApplication.processEvents()

        try:
            for n, source in enumerate(selected):
                source_count = max(0, int(source["layer"].featureCount()))
                self.progress.setValue(
                    min(99, int(completed_work * 100 / total_work))
                )
                QApplication.processEvents()

                self._log(
                    "\n" + "=" * 72 + "\n"
                    "SOURCE %d/%d\n%s" %
                    (
                        n + 1,
                        len(selected),
                        self._target_resolution_diagnostic(source)
                    )
                )

                # Use exactly the target displayed in the main matrix.
                target = self._source_target_map.get(source["layer"].id())
                target_from_matrix = target is not None
                if target is None:
                    target = self._resolve_target(source)

                if target is None:
                    reason = (
                        "工程1存在多个同名目标图层，无法唯一确定"
                        if self._last_resolution_error == "MULTIPLE_TARGETS"
                        else "未找到工程1目标图层"
                    )
                    text_value = (
                        "%s :: %s → %s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            reason
                        )
                    )
                    skipped_layers.append(text_value)
                    self._log(text_value, Qgis.Warning)
                    completed_work += source_count
                    self.progress.setValue(
                        min(99, int(completed_work * 100 / total_work))
                    )
                    QApplication.processEvents()
                    continue

                resolved_count += 1

                self._log(
                    "[TARGET CHOSEN] matrix=%s | target_id=%s | target_name=%r | source_id=%s"
                    % (
                        target_from_matrix,
                        target.id(),
                        target.name(),
                        source["layer"].id()
                    )
                )

                compare = self._geometry_compare(target, source["layer"])
                diagnostic = self._format_geometry_diagnostic(compare)
                field_map = self._field_mapping_diagnostic(
                    target, source["layer"]
                )
                self._log("[GEOMETRY CHECK]\n" + diagnostic)
                self._log(
                    "[FIELD MAPPING]\n"
                    "common=%s\n"
                    "target_only=%s\n"
                    "source_only=%s\n"
                    "type_conflicts=%s\n"
                    "target_fields=%s\n"
                    "source_fields=%s"
                    % (
                        field_map["common"],
                        field_map["target_only"],
                        field_map["source_only"],
                        field_map["type_conflicts"],
                        field_map["target_fields"],
                        field_map["source_fields"]
                    )
                )
                self._log(
                    "[FEATURE COUNTS BEFORE] target=%d source=%d"
                    % (
                        target.featureCount(),
                        source["layer"].featureCount()
                    )
                )

                if not compare["compatible"]:
                    geometry_fail_count += 1
                    errors.append(
                        "%s :: %s → 几何类型不兼容\\n%s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            diagnostic
                        )
                    )
                    self._log(
                        "[GEOMETRY FAIL] %s :: %s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name()
                        ),
                        Qgis.Warning
                    )
                    continue

                geometry_pass_count += 1

                if not compare["target_valid"] or not compare["source_valid"]:
                    invalid_text = (
                        "%s :: %s → 图层无效\\n%s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            diagnostic
                        )
                    )
                    invalid_count += 1
                    completed_work += source_count
                    self.progress.setValue(
                        min(99, int(completed_work * 100 / total_work))
                    )
                    QApplication.processEvents()
                    errors.append(invalid_text)
                    self._log(invalid_text, Qgis.Warning)
                    continue

                try:
                    source_start_work = completed_work

                    def update_feature_progress(processed, source_total):
                        if source_total <= 0:
                            return
                        current = source_start_work + processed
                        self.progress.setValue(
                            min(99, int(current * 100 / total_work))
                        )

                    added = self._append_features(
                        target,
                        source["layer"],
                        progress_callback=update_feature_progress
                    )
                    completed_work += source_count
                    self.progress.setValue(
                        min(99, int(completed_work * 100 / total_work))
                    )
                    QApplication.processEvents()
                    total_added += added
                    merge_pass_count += 1
                    self._log(
                        "[MERGE PASS] %s -> %s | added=%d | target_after=%d"
                        % (
                            source["layer"].name(),
                            target.name(),
                            added,
                            target.featureCount()
                        )
                    )
                except Exception as exc:
                    write_fail_count += 1
                    compare_text = self._format_geometry_diagnostic(compare)
                    fail_text = (
                        "%s :: %s → 写入失败：%s\\n%s" %
                        (
                            Path(source["folder"]).name,
                            source["layer"].name(),
                            str(exc),
                            compare_text
                        )
                    )
                    self._log("[MERGE FAIL]\n" + fail_text, Qgis.Critical)
                    completed_work += source_count
                    self.progress.setValue(
                        min(99, int(completed_work * 100 / total_work))
                    )
                    QApplication.processEvents()
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

            # “参与图层” = 当前选中文件夹中的源图层名称已经有
            # Engineering 1 目标。包含同名自动匹配，也包含“移至/新增”映射。
            participating_names = set()
            unresolved_names = set()
            current_targets = self._target_layers()

            for source in selected:
                name = self._normalized_name(source["layer"].name())
                if source["layer"].id() in self._source_target_map:
                    participating_names.add(name)
                    continue

                candidates = current_targets.get(name, [])
                mapping = self.mappings.get(name)

                if len(candidates) == 1:
                    participating_names.add(name)
                    continue

                if mapping:
                    action = mapping.get("action")
                    mapped_name = self._normalized_name(mapping.get("target"))
                    if action == "move" and len(
                        current_targets.get(mapped_name, [])
                    ) == 1:
                        participating_names.add(name)
                        continue
                    if action == "new" and len(candidates) == 1:
                        participating_names.add(name)
                        continue

                unresolved_names.add(name)

            summary_log = (
                "Merge and Split %s\n"
                "参与文件夹=%d\n"
                "源图层总数=%d\n"
                "参与图层=%d\n"
                "未找到目标=%d\n"
                "错误=%d\n"
                "目标解析=%d\n"
                "几何通过=%d\n"
                "几何失败=%d\n"
                "图层无效=%d\n"
                "写入成功=%d\n"
                "写入失败=%d\n"
                "参与图层名称=%s\n"
                "未设置目标图层名称=%s\n"
                "注：字段类型冲突按目标字段尝试转换，无法转换的值写 NULL\n"
                "新增要素=%d"
                % (
                    PLUGIN_VERSION,
                    sum(1 for enabled in self.folder_enabled if enabled),
                    len(selected),
                    len(participating_names),
                    len(skipped_layers),
                    len(errors),
                    resolved_count,
                    geometry_pass_count,
                    geometry_fail_count,
                    invalid_count,
                    merge_pass_count,
                    write_fail_count,
                    ", ".join(sorted(participating_names)),
                    ", ".join(sorted(unresolved_names)),
                    total_added
                )
            )
            self._log("\n" + "#" * 72 + "\n" + summary_log + "\n" + "#" * 72)

            message = (
                "合并完成。\n\n"
                "参与文件夹：%d\n"
                "参与图层：%d\n"
                "新增要素：%d"
                % (
                    sum(1 for enabled in self.folder_enabled if enabled),
                    len(participating_names),
                    total_added
                )
            )

            if errors:
                message += (
                    "\n\n处理诊断：\n"
                    "目标解析：%d\n"
                    "几何通过：%d\n"
                    "几何失败：%d\n"
                    "图层无效：%d\n"
                    "写入成功：%d\n"
                    "写入失败：%d"
                    % (
                        resolved_count,
                        geometry_pass_count,
                        geometry_fail_count,
                        invalid_count,
                        merge_pass_count,
                        write_fail_count
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

from qgis.PyQt.QtWidgets import QDialog, QVBoxLayout, QHBoxLayout, QListWidget, QPushButton, QLabel, QFileDialog, QMessageBox, QProgressBar, QCheckBox
from qgis.PyQt.QtCore import Qt
from qgis.core import QgsProject, QgsVectorLayer, QgsFeature, QgsCoordinateTransform

class MergeSplitDialog(QDialog):
    def __init__(self, iface, parent=None):
        super().__init__(parent)
        self.iface = iface
        self.setWindowTitle('Merge and Split - 合并')
        self.resize(760, 520)
        self._build_ui()

    def _build_ui(self):
        layout = QVBoxLayout(self)
        layout.addWidget(QLabel('<b>QGIS 文件合并</b><br>将所选工程/单图层中的要素，按“图层名称”汇总到当前打开工程的同名图层。'))
        self.target_label = QLabel()
        layout.addWidget(self.target_label)
        self._refresh_target_label()

        row = QHBoxLayout()
        self.add_btn = QPushButton('添加文件…')
        self.remove_btn = QPushButton('移除选中')
        self.clear_btn = QPushButton('清空')
        self.add_btn.clicked.connect(self.add_files)
        self.remove_btn.clicked.connect(self.remove_selected)
        self.clear_btn.clicked.connect(self.clear_files)
        row.addWidget(self.add_btn)
        row.addWidget(self.remove_btn)
        row.addWidget(self.clear_btn)
        row.addStretch()
        layout.addLayout(row)

        self.files = QListWidget()
        self.files.setSelectionMode(QListWidget.ExtendedSelection)
        layout.addWidget(self.files, 1)

        self.skip_unmatched = QCheckBox('跳过目标工程中不存在的图层（推荐）')
        self.skip_unmatched.setChecked(True)
        layout.addWidget(self.skip_unmatched)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)

        bottom = QHBoxLayout()
        bottom.addStretch()
        self.merge_btn = QPushButton('开始合并')
        self.close_btn = QPushButton('关闭')
        self.merge_btn.clicked.connect(self.merge)
        self.close_btn.clicked.connect(self.close)
        bottom.addWidget(self.merge_btn)
        bottom.addWidget(self.close_btn)
        layout.addLayout(bottom)

    def _refresh_target_label(self):
        path = QgsProject.instance().fileName()
        self.target_label.setText('<b>总文件：</b>' + (path if path else '当前工程（尚未保存）'))

    def add_files(self):
        paths, _ = QFileDialog.getOpenFileNames(self, '选择QGIS工程或单图层文件', '', 'QGIS/矢量文件 (*.qgz *.qgs *.gpkg *.shp *.geojson *.json *.sqlite *.kml *.kmz);;所有文件 (*.*)')
        existing = {self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())}
        for path in paths:
            if path not in existing:
                self.files.addItem(path)
                self.files.item(self.files.count() - 1).setData(Qt.UserRole, path)

    def remove_selected(self):
        for item in self.files.selectedItems():
            self.files.takeItem(self.files.row(item))

    def clear_files(self):
        self.files.clear()

    def _target_layers(self):
        result = {}
        for layer in QgsProject.instance().mapLayers().values():
            if isinstance(layer, QgsVectorLayer):
                result.setdefault(layer.name(), []).append(layer)
        return result

    def _load_source_layers(self, path):
        if path.lower().endswith(('.qgz', '.qgs')):
            project = QgsProject()
            if not project.read(path):
                raise RuntimeError('无法读取QGIS工程：' + path)
            return list(project.mapLayers().values())
        layer = QgsVectorLayer(path, '', 'ogr')
        if not layer.isValid():
            raise RuntimeError('无法读取图层：' + path)
        return [layer]

    def _compatible(self, target, source):
        if target.geometryType() != source.geometryType():
            return False
        t_fields = {f.name(): f.type() for f in target.fields()}
        s_fields = {f.name(): f.type() for f in source.fields()}
        return all(name in t_fields for name in s_fields)

    def _append_features(self, target, source):
        transform = None
        if target.crs().isValid() and source.crs().isValid() and target.crs() != source.crs():
            transform = QgsCoordinateTransform(source.crs(), target.crs(), QgsProject.instance())
        was_editing = target.isEditable()
        if not was_editing and not target.startEditing():
            raise RuntimeError('无法进入编辑状态：' + target.name())

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
            feat.setAttributes([src_feat[source_index[field.name()]] if field.name() in source_index else None for field in fields])
            if not target.addFeature(feat):
                raise RuntimeError('写入图层失败：' + target.name())
            count += 1
        if not was_editing and not target.commitChanges():
            raise RuntimeError('提交失败：' + target.name() + '\n' + '; '.join(target.commitErrors()))
        return count

    def merge(self):
        if self.files.count() == 0:
            QMessageBox.warning(self, '提示', '请先选择至少一个工程或单图层文件。')
            return
        project = QgsProject.instance()
        if not project.mapLayers():
            QMessageBox.warning(self, '提示', '当前工程没有图层，无法作为总文件。')
            return
        target_layers = self._target_layers()
        paths = [self.files.item(i).data(Qt.UserRole) for i in range(self.files.count())]
        total_added = 0
        matched_layers = 0
        skipped_layers = []
        errors = []
        self.merge_btn.setEnabled(False)
        self.progress.setValue(0)
        try:
            for n, path in enumerate(paths):
                self.progress.setValue(int(n * 100 / max(1, len(paths))))
                try:
                    sources = self._load_source_layers(path)
                    for source in sources:
                        if not isinstance(source, QgsVectorLayer):
                            continue
                        targets = target_layers.get(source.name(), [])
                        if not targets:
                            skipped_layers.append(path + ' :: ' + source.name())
                            continue
                        target = targets[0]
                        if not self._compatible(target, source):
                            errors.append(path + ' :: ' + source.name() + ' → 字段/几何类型不兼容')
                            continue
                        total_added += self._append_features(target, source)
                        matched_layers += 1
                except Exception as exc:
                    errors.append(path + ' :: ' + str(exc))
            project.setDirty(True)
            self.progress.setValue(100)
            message = '合并完成。\n\n处理文件：%d\n匹配图层：%d\n新增要素：%d' % (len(paths), matched_layers, total_added)
            if skipped_layers:
                message += '\n\n跳过未匹配图层：%d' % len(skipped_layers)
            if errors:
                message += '\n\n发生错误：%d\n%s' % (len(errors), '\n'.join(errors[:10]))
            QMessageBox.information(self, 'Merge and Split', message)
            self.iface.mapCanvas().refresh()
        finally:
            self.merge_btn.setEnabled(True)

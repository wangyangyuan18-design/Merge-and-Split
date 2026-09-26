from pathlib import Path

from qgis.PyQt.QtWidgets import QAction
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtCore import QCoreApplication
from .merge_dialog import MergeSplitDialog
from .split_dialog import SplitDialog


class MergeAndSplit:
    def __init__(self, iface):
        self.iface = iface
        self.action = None
        self.split_action = None
        self.dialog = None
        self.split_dialog = None

    def initGui(self):
        icon_path = str(Path(__file__).with_name("icon.svg"))

        self.action = QAction(
            QIcon(icon_path),
            QCoreApplication.translate("MergeAndSplit", "Merge and Split"),
            self.iface.mainWindow()
        )
        self.action.triggered.connect(self.run)
        self.iface.addPluginToMenu("&Merge and Split", self.action)
        self.iface.addToolBarIcon(self.action)

        self.split_action = QAction(
            QIcon(icon_path),
            QCoreApplication.translate(
                "MergeAndSplit", "Split / 区域拆分"
            ),
            self.iface.mainWindow()
        )
        self.split_action.triggered.connect(self.run_split)
        self.iface.addPluginToMenu("&Merge and Split", self.split_action)
        self.iface.addToolBarIcon(self.split_action)

    def unload(self):
        if self.action:
            self.iface.removePluginMenu("&Merge and Split", self.action)
            self.iface.removeToolBarIcon(self.action)

        if self.split_action:
            self.iface.removePluginMenu(
                "&Merge and Split", self.split_action
            )
            self.iface.removeToolBarIcon(self.split_action)

        self.dialog = None
        self.split_dialog = None

    def run(self):
        if self.dialog is None:
            self.dialog = MergeSplitDialog(
                self.iface, self.iface.mainWindow()
            )
        self.dialog.show()
        self.dialog.raise_()
        self.dialog.activateWindow()

    def run_split(self):
        if self.split_dialog is None:
            self.split_dialog = SplitDialog(
                self.iface, self.iface.mainWindow()
            )
        self.split_dialog.show()
        self.split_dialog.raise_()
        self.split_dialog.activateWindow()

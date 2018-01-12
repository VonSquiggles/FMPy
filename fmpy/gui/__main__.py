""" Entry point for the graphical user interface """

try:
    from . import compile_resources
    compile_resources()
except Exception as e:
    print("Failed to compiled resources. %s" % e)

import os

from PyQt5.QtCore import QCoreApplication, QDir, Qt, pyqtSignal, QUrl, QSettings, QPoint, QTimer, QDateTime
from PyQt5.QtWidgets import QApplication, QMainWindow, QWidget, QLineEdit, QComboBox, QFileDialog, QLabel, QVBoxLayout, QMenu, QMessageBox, QProgressDialog, QProgressBar
from PyQt5.QtGui import QDesktopServices, QPixmap, QIcon, QDoubleValidator

from fmpy.gui.generated.MainWindow import Ui_MainWindow
import fmpy
from fmpy import read_model_description, supported_platforms, platform
from fmpy.model_description import ScalarVariable


from .model import VariablesTableModel, VariablesTreeModel, VariablesModel, VariablesFilterModel
from .log import Log

QCoreApplication.setApplicationVersion(fmpy.__version__)
QCoreApplication.setOrganizationName("CATIA-Systems")
QCoreApplication.setApplicationName("FMPy")

import pyqtgraph as pg

pg.setConfigOptions(background='w', foreground='k', antialias=True)


class ClickableLabel(QLabel):
    """ A QLabel that shows a pointing hand cursor and emits a *clicked* event when clicked """

    clicked = pyqtSignal()

    def __init__(self, parent=None):
        super(ClickableLabel, self).__init__(parent)
        self.setCursor(Qt.PointingHandCursor)

    def mousePressEvent(self, ev):
        self.clicked.emit()
        super(ClickableLabel, self).mousePressEvent(ev)


class MainWindow(QMainWindow):

    variableSelected = pyqtSignal(ScalarVariable, name='variableSelected')
    variableDeselected = pyqtSignal(ScalarVariable, name='variableDeselected')
    windows = []
    windowOffset = QPoint()

    def __init__(self, parent=None):
        super(MainWindow, self).__init__(parent)

        # save from garbage collection
        self.windows.append(self)

        # state
        self.filename = None
        self.result = None
        self.modelDescription = None
        self.variables = dict()
        self.selectedVariables = set()
        self.startValues = dict()
        self.simulationThread = None
        # self.progressDialog = None
        self.plotUpdateTimer = QTimer(self)
        self.plotUpdateTimer.timeout.connect(self.updatePlotData)
        self.curves = []

        # UI
        self.ui = Ui_MainWindow()
        self.ui.setupUi(self)

        # set the window size to 85% of the available space
        geo = QApplication.desktop().availableGeometry()
        width = min(geo.width() * 0.85, 1100.0)
        height = min(geo.height() * 0.85, 900.0)
        self.resize(width, height)

        # hide the variables
        self.ui.dockWidget.hide()

        # toolbar
        self.stopTimeLineEdit = QLineEdit("1")
        self.stopTimeLineEdit.setToolTip("Stop time")
        self.stopTimeLineEdit.setFixedWidth(50)
        self.stopTimeValidator = QDoubleValidator(self)
        self.stopTimeValidator.setBottom(0)
        self.stopTimeLineEdit.setValidator(self.stopTimeValidator)

        self.ui.toolBar.addWidget(self.stopTimeLineEdit)

        spacer = QWidget(self)
        spacer.setFixedWidth(10)
        self.ui.toolBar.addWidget(spacer)

        self.fmiTypeComboBox = QComboBox(self)
        self.fmiTypeComboBox.addItem("Co-Simulation")
        self.fmiTypeComboBox.setToolTip("FMI type")
        self.fmiTypeComboBox.setSizeAdjustPolicy(QComboBox.AdjustToContents)
        self.ui.toolBar.addWidget(self.fmiTypeComboBox)

        # disable widgets
        self.ui.actionSettings.setEnabled(False)
        self.ui.actionShowLog.setEnabled(False)
        self.ui.actionShowResults.setEnabled(False)
        self.ui.actionSimulate.setEnabled(False)
        self.stopTimeLineEdit.setEnabled(False)
        self.fmiTypeComboBox.setEnabled(False)

        # hide the dock's title bar
        self.ui.dockWidget.setTitleBarWidget(QWidget())

        self.ui.tableView.setMinimumWidth(500)

        self.model = VariablesTableModel(self.selectedVariables, self.startValues)
        self.tableFilterModel = VariablesFilterModel()
        self.tableFilterModel.setSourceModel(self.model)
        self.tableFilterModel.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.ui.tableView.setModel(self.tableFilterModel)

        self.treeModel = VariablesTreeModel(self.selectedVariables, self.startValues)
        self.treeFilterModel = VariablesFilterModel()
        self.treeFilterModel.setSourceModel(self.treeModel)
        self.treeFilterModel.setFilterCaseSensitivity(Qt.CaseInsensitive)
        self.ui.treeView.setModel(self.treeFilterModel)

        for i, (w, n) in enumerate(zip(VariablesModel.COLUMN_WIDTHS, VariablesModel.COLUMN_NAMES)):
            self.ui.treeView.setColumnWidth(i, w)
            self.ui.tableView.setColumnWidth(i, w)

            if n in ['Value Reference', 'Initial', 'Causality', 'Variability']:
                self.ui.treeView.setColumnHidden(i, True)
                self.ui.tableView.setColumnHidden(i, True)

        # populate the recent files list
        settings = QSettings()
        recent_files = settings.value("recentFiles", defaultValue=[])
        vbox = QVBoxLayout()

        if recent_files:
            added = set()
            for file in recent_files[:5]:
                if file in added:
                    continue  # skip duplicates
                link = QLabel('<a href="%s" style="text-decoration: none">%s</a>' % (file, os.path.basename(file)))
                link.setToolTip(file)
                link.linkActivated.connect(self.load)
                vbox.addWidget(link)
                added.add(file)

        self.ui.recentFilesGroupBox.setLayout(vbox)
        self.ui.recentFilesGroupBox.setVisible(len(recent_files) > 0)

        # log page
        self.log = Log(self)
        self.ui.logTreeView.setModel(self.log)
        self.ui.clearLogButton.clicked.connect(self.log.clear)

        # context menu
        self.contextMenu = QMenu()
        self.actionExpandAll = self.contextMenu.addAction("Expand all")
        self.actionExpandAll.triggered.connect(self.ui.treeView.expandAll)
        self.actionCollapseAll = self.contextMenu.addAction("Collapse all")
        self.actionCollapseAll.triggered.connect(self.ui.treeView.collapseAll)
        self.contextMenu.addSeparator()
        for column in ['Value Reference', 'Initial', 'Causality', 'Variability']:
            action = self.contextMenu.addAction(column)
            action.setCheckable(True)
            action.toggled.connect(lambda show, col=column: self.showColumn(col, show))

        # file menu
        self.ui.actionExit.triggered.connect(QApplication.closeAllWindows)

        # help menu
        self.ui.actionOpenFMI1SpecCS.triggered.connect(lambda: QDesktopServices.openUrl(QUrl('https://svn.modelica.org/fmi/branches/public/specifications/v1.0/FMI_for_CoSimulation_v1.0.1.pdf')))
        self.ui.actionOpenFMI1SpecME.triggered.connect(lambda: QDesktopServices.openUrl(QUrl('https://svn.modelica.org/fmi/branches/public/specifications/v1.0/FMI_for_ModelExchange_v1.0.1.pdf')))
        self.ui.actionOpenFMI2Spec.triggered.connect(lambda: QDesktopServices.openUrl(QUrl('https://svn.modelica.org/fmi/branches/public/specifications/v2.0/FMI_for_ModelExchange_and_CoSimulation_v2.0.pdf')))
        self.ui.actionOpenTestFMUs.triggered.connect(lambda: QDesktopServices.openUrl(QUrl('https://trac.fmi-standard.org/browser/branches/public/Test_FMUs')))

        # filter menu
        self.filterMenu = QMenu()
        self.filterMenu.addAction(self.ui.actionFilterInputs)
        self.filterMenu.addAction(self.ui.actionFilterOutputs)
        self.filterMenu.addAction(self.ui.actionFilterParameters)
        self.filterMenu.addAction(self.ui.actionFilterCalculatedParameters)
        self.filterMenu.addAction(self.ui.actionFilterIndependentVariables)
        self.filterMenu.addAction(self.ui.actionFilterLocalVariables)
        self.ui.filterToolButton.setMenu(self.filterMenu)

        # status bar
        self.statusIconLabel = ClickableLabel(self)
        self.statusIconLabel.setStyleSheet("QLabel { margin-left: 5px; }")
        self.statusIconLabel.clicked.connect(self.showLogPage)
        self.ui.statusBar.addPermanentWidget(self.statusIconLabel)

        self.statusTextLabel = ClickableLabel(self)
        self.statusTextLabel.setMinimumWidth(10)
        self.statusTextLabel.clicked.connect(self.showLogPage)
        self.ui.statusBar.addPermanentWidget(self.statusTextLabel)

        self.ui.statusBar.addPermanentWidget(QWidget(self), 1)  # spacer

        self.simulationProgressBar = QProgressBar(self)
        self.simulationProgressBar.setFixedHeight(18)
        self.ui.statusBar.addPermanentWidget(self.simulationProgressBar)
        self.simulationProgressBar.setVisible(False)

        # connect signals and slots
        self.ui.actionNewWindow.triggered.connect(self.newWindow)
        self.ui.openButton.clicked.connect(self.open)
        self.ui.actionOpen.triggered.connect(self.open)
        self.ui.actionSimulate.triggered.connect(self.startSimulation)
        self.ui.actionSettings.triggered.connect(self.showSettingsPage)
        self.ui.actionShowLog.triggered.connect(self.showLogPage)
        self.ui.actionShowResults.triggered.connect(self.showResultPage)
        self.fmiTypeComboBox.currentTextChanged.connect(self.updateSimulationSettings)
        self.ui.solverComboBox.currentTextChanged.connect(self.updateSimulationSettings)
        self.variableSelected.connect(self.updatePlotLayout)
        self.variableDeselected.connect(self.updatePlotLayout)
        self.model.variableSelected.connect(self.selectVariable)
        self.model.variableDeselected.connect(self.deselectVariable)
        self.treeModel.variableSelected.connect(self.selectVariable)
        self.treeModel.variableDeselected.connect(self.deselectVariable)
        self.ui.filterLineEdit.textChanged.connect(self.treeFilterModel.setFilterFixedString)
        self.ui.filterLineEdit.textChanged.connect(self.tableFilterModel.setFilterFixedString)
        self.ui.filterToolButton.toggled.connect(self.treeFilterModel.setFilterByCausality)
        self.ui.filterToolButton.toggled.connect(self.tableFilterModel.setFilterByCausality)
        self.log.currentMessageChanged.connect(self.setStatusMessage)

        self.ui.tableViewToolButton.toggled.connect(lambda show: self.ui.variablesStackedWidget.setCurrentWidget(self.ui.tablePage if show else self.ui.treePage))

        for model in [self.treeFilterModel, self.tableFilterModel]:
            self.ui.actionFilterInputs.triggered.connect(model.setFilterInputs)
            self.ui.actionFilterOutputs.triggered.connect(model.setFilterOutputs)
            self.ui.actionFilterParameters.triggered.connect(model.setFilterParameters)
            self.ui.actionFilterCalculatedParameters.triggered.connect(model.setFilterCalculatedParameters)
            self.ui.actionFilterIndependentVariables.triggered.connect(model.setFilterIndependentVariables)
            self.ui.actionFilterLocalVariables.triggered.connect(model.setFilterLocalVariables)

        self.ui.treeView.customContextMenuRequested.connect(self.showContextMenu)
        self.ui.tableView.customContextMenuRequested.connect(self.showContextMenu)

    def newWindow(self):
        window = MainWindow()
        window.show()

    def show(self):
        super(MainWindow, self).show()
        self.move(self.frameGeometry().topLeft() + self.windowOffset)
        self.windowOffset += QPoint(20, 20)

    def showContextMenu(self, point):
        """ Update and show the variables context menu """

        if self.ui.variablesStackedWidget.currentWidget() == self.ui.treePage:
            currentView = self.ui.treeView
        else:
            currentView = self.ui.tableView

        self.actionExpandAll.setEnabled(currentView == self.ui.treeView)
        self.actionCollapseAll.setEnabled(currentView == self.ui.treeView)

        self.contextMenu.exec_(currentView.mapToGlobal(point))

    def load(self, filename):

        if not self.isVisible():
            self.show()

        try:
            self.modelDescription = md = read_model_description(filename)
        except:
            QMessageBox.warning(self, "Failed to load FMU", "Failed to load %s" % filename)
            return

        self.filename = filename
        platforms = supported_platforms(self.filename)

        self.variables.clear()
        self.selectedVariables.clear()

        for v in md.modelVariables:
            self.variables[v.name] = v
            if v.causality == 'output':
                self.selectedVariables.add(v)

        fmi_types = []
        if md.coSimulation:
            fmi_types.append('Co-Simulation')
        if md.modelExchange:
            fmi_types.append('Model Exchange')

        # toolbar
        if md.defaultExperiment is not None:
            if md.defaultExperiment.stopTime is not None:
                self.stopTimeLineEdit.setText(str(md.defaultExperiment.stopTime))

        # variables view
        self.model.modelDescription = md
        self.model.setModelDescription(md)
        self.treeModel.setModelDescription(md)
        self.treeFilterModel.invalidate()
        self.tableFilterModel.invalidate()
        self.ui.tableView.reset()
        self.ui.treeView.reset()

        # settings page
        self.ui.fmiVersionLabel.setText(md.fmiVersion)
        self.ui.fmiTypeLabel.setText(', '.join(fmi_types))
        self.ui.platformsLabel.setText(', '.join(platforms))
        self.ui.modelNameLabel.setText(md.modelName)
        self.ui.descriptionLabel.setText(md.description)
        self.ui.numberOfContinuousStatesLabel.setText(str(md.numberOfContinuousStates))
        self.ui.numberOfEventIndicatorsLabel.setText(str(md.numberOfEventIndicators))
        self.ui.numberOfVariablesLabel.setText(str(len(md.modelVariables)))
        self.ui.generationToolLabel.setText(md.generationTool)
        self.ui.generationDateAndTimeLabel.setText(md.generationDateAndTime)

        self.fmiTypeComboBox.clear()
        self.fmiTypeComboBox.addItems(fmi_types)

        self.updateSimulationSettings()

        self.ui.stackedWidget.setCurrentWidget(self.ui.settingsPage)

        self.ui.dockWidget.show()

        self.ui.actionSettings.setEnabled(True)
        self.ui.actionShowLog.setEnabled(True)

        can_simulate = platform in platforms

        self.ui.actionSimulate.setEnabled(can_simulate)
        self.stopTimeLineEdit.setEnabled(can_simulate)
        self.fmiTypeComboBox.setEnabled(can_simulate and len(fmi_types) > 1)
        self.ui.settingsGroupBox.setEnabled(can_simulate)

        settings = QSettings()
        recent_files = settings.value("recentFiles", defaultValue=[])

        # save the 10 most recent files
        settings.setValue('recentFiles', [filename] + recent_files[:9])

        self.setWindowTitle("%s - FMPy" % os.path.basename(filename))

    def open(self):

        start_dir = QDir.homePath()

        settings = QSettings()
        recent_files = settings.value("recentFiles", defaultValue=[])

        for filename in recent_files:
            dirname = os.path.dirname(filename)
            if os.path.isdir(dirname):
                start_dir = dirname
                break

        filename, _ = QFileDialog.getOpenFileName(parent=self,
                                                  caption="Open File",
                                                  directory=start_dir,
                                                  filter="FMUs (*.fmu);;All Files (*.*)")

        if filename:
            self.load(filename)

    def showSettingsPage(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.settingsPage)

    def showLogPage(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.logPage)

    def showResultPage(self):
        self.ui.stackedWidget.setCurrentWidget(self.ui.resultPage)

    def updateSimulationSettings(self):

        if self.fmiTypeComboBox.currentText() == 'Co-Simulation':
            self.ui.solverComboBox.setEnabled(False)
            self.ui.stepSizeLineEdit.setEnabled(False)
            self.ui.relativeToleranceLineEdit.setEnabled(False)
        else:
            self.ui.solverComboBox.setEnabled(True)
            fixed_step = self.ui.solverComboBox.currentText() == 'Fixed-step'
            self.ui.stepSizeLineEdit.setEnabled(fixed_step)
            self.ui.relativeToleranceLineEdit.setEnabled(not fixed_step)

    def selectVariable(self, variable):
        self.selectedVariables.add(variable)
        self.variableSelected.emit(variable)

    def deselectVariable(self, variable):
        self.selectedVariables.remove(variable)
        self.variableDeselected.emit(variable)

    def startSimulation(self):

        from .simulation import SimulationThread

        # TODO: catch exceptions
        stop_time = float(self.stopTimeLineEdit.text())
        step_size = float(self.ui.stepSizeLineEdit.text())
        relative_tolerance = float(self.ui.relativeToleranceLineEdit.text())
        max_samples = float(self.ui.maxSamplesLineEdit.text())

        output_interval = stop_time / max_samples

        if self.ui.solverComboBox.currentText() == 'Fixed-step':
            solver = 'Euler'
        else:
            solver = 'CVode'

        output = []
        for variable in self.modelDescription.modelVariables:
            output.append(variable.name)

        self.simulationThread = SimulationThread(filename=self.filename,
                                                 stopTime=stop_time,
                                                 solver=solver,
                                                 stepSize=step_size,
                                                 relativeTolerance=relative_tolerance,
                                                 outputInterval=output_interval,
                                                 startValues=self.startValues,
                                                 output=output)

        self.ui.actionSimulate.setIcon(QIcon(':/icons/stop.png'))
        self.ui.actionSimulate.setToolTip("Stop simulation")
        self.ui.actionSimulate.triggered.disconnect(self.startSimulation)
        self.ui.actionSimulate.triggered.connect(self.simulationThread.stop)

        self.simulationProgressBar.setVisible(True)

        self.simulationThread.messageChanged.connect(self.log.log)
        self.simulationThread.progressChanged.connect(self.simulationProgressBar.setValue)
        self.simulationThread.finished.connect(self.simulationFinished)

        if self.ui.clearLogOnStartButton.isChecked():
            self.log.clear()

        self.showResultPage()

        self.simulationThread.start()
        self.plotUpdateTimer.start(100)

        self.updatePlotLayout()

    def simulationFinished(self):

        # update UI
        self.ui.actionSimulate.triggered.disconnect(self.simulationThread.stop)
        self.ui.actionSimulate.triggered.connect(self.startSimulation)
        self.ui.actionSimulate.setIcon(QIcon(':/icons/play.png'))
        self.ui.actionSimulate.setToolTip("Start simulation")
        self.plotUpdateTimer.stop()
        self.simulationProgressBar.setVisible(False)
        self.ui.actionShowResults.setEnabled(True)
        self.ui.actionSettings.setEnabled(True)
        self.ui.stackedWidget.setCurrentWidget(self.ui.resultPage)
        self.updatePlotLayout()

        if self.result is None:
            self.showLogPage()

        self.result = self.simulationThread.result

        self.simulationThread = None

    def updatePlotData(self):

        import numpy as np

        if self.simulationThread is None or len(self.simulationThread.rows) < 2:
            return

        self.result = np.array(self.simulationThread.rows, dtype=np.dtype(self.simulationThread.cols))

        time = self.result['time']

        for variable, curve in self.curves:

            if variable.name not in self.result.dtype.names:
                continue

            y = self.result[variable.name]

            if variable.type == 'Real':
                curve.setData(x=time, y=y)
            else:
                curve.setData(x=time, y=y[:-1], stepMode=True)

    def updatePlotLayout(self):

        self.ui.plotWidget.clear()

        self.curves.clear()

        if self.simulationThread is not None:
            stop_time = self.simulationThread.stopTime
        else:
            stop_time = 1.0

        for variable in self.selectedVariables:

            self.ui.plotWidget.nextRow()
            plot = self.ui.plotWidget.addPlot()

            if variable.type == 'Real':
                curve = plot.plot(pen=(0, 0, 255))
            else:
                if variable.type == 'Boolean':
                    brush = (0, 0, 255, 50)
                    plot.setYRange(0, 1, padding=0.2)
                    plot.getAxis('left').setTicks([[(0, 'false'), (1, 'true')], []])
                else:
                    brush = None
                curve = plot.plot(pen=(0, 0, 255), fillLevel=0, fillBrush=brush, antialias=False)

            plot.setXRange(0, stop_time, padding=0.05)

            plot.setLabel('left', variable.name)
            plot.showGrid(x=True, y=True, alpha=0.25)

            # hide the auto-scale button and disable context menu and mouse interaction
            plot.hideButtons()
            plot.setMouseEnabled(False, False)
            plot.setMenuEnabled(False)

            self.curves.append((variable, curve))

        self.updatePlotData()

    def showColumn(self, name, show):
        i = VariablesModel.COLUMN_NAMES.index(name)
        self.ui.treeView.setColumnHidden(i, not show)
        self.ui.tableView.setColumnHidden(i, not show)

    def setStatusMessage(self, level, text):
        self.statusIconLabel.setPixmap(QPixmap(':/icons/%s-16x16.png' % level))
        self.statusTextLabel.setText(text)

    def dragEnterEvent(self, event):

        for url in event.mimeData().urls():
            if not url.isLocalFile():
                return

        event.acceptProposedAction()

    def dropEvent(self, event):

        urls = event.mimeData().urls()

        for url in urls:
            if url == urls[0]:
                window = self
            else:
                window = MainWindow()
                
            window.load(url.toLocalFile())


if __name__ == '__main__':

    import sys

    app = QApplication(sys.argv)
    window = MainWindow()
    window.show()

    for i, v in enumerate(sys.argv[1:]):
        if i > 0:
            window = MainWindow()
            window.show()
        window.load(v)

    sys.exit(app.exec_())
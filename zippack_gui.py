# -*- coding: utf-8 -*-
"""
分包打包器 —— 把一个大文件夹按体积上限、按顺序切成若干个压缩包
适合微信/邮件发送（单个文件有大小限制）的场景

依赖：PySide6
"""
from __future__ import annotations

import os
import sys
import time
import traceback

from PySide6.QtCore import Qt, QThread, Signal, QTimer, QSize
from PySide6.QtGui import QFont, QIcon, QPixmap, QPainter, QColor, QBrush, QPen, QAction
from PySide6.QtWidgets import (
    QApplication, QMainWindow, QWidget, QVBoxLayout, QHBoxLayout, QGridLayout,
    QLabel, QLineEdit, QPushButton, QComboBox, QCheckBox, QDoubleSpinBox,
    QProgressBar, QTextEdit, QFileDialog, QFrame, QDialog, QTableWidget,
    QTableWidgetItem, QHeaderView, QMessageBox, QAbstractItemView, QSizePolicy, QScrollArea,
    QSpacerItem, QToolButton,
)

import zippack_core as Z

APP_TITLE = "分包打包器"
APP_SUB = "按顺序把大文件夹切成若干个不超过指定体积的压缩包"
ACCENT = "#2F6FED"
QSS = """
QWidget { font-family: "Microsoft YaHei UI","Microsoft YaHei",sans-serif; font-size: 12px; color: #1F2329; }
QMainWindow, #root { background: #F5F6F8; }
QFrame#card { background: #FFFFFF; border: 1px solid #E4E7EC; border-radius: 10px; }
QLabel#cardTitle { font-size: 13px; font-weight: 600; color: #1F2329; }
QLabel#hint { color: #8A9099; font-size: 11px; }
QLabel#title { font-size: 20px; font-weight: 600; color: #12161C; }
QLabel#sub { color: #6B7280; font-size: 12px; }
QLabel#big { font-size: 12px; font-weight: 600; color: #2F6FED; }
QLineEdit, QComboBox, QDoubleSpinBox {
    background: #FFFFFF; border: 1px solid #D8DDE5; border-radius: 6px;
    padding: 5px 8px; min-height: 20px; selection-background-color: #DCE8FF;
}
QLineEdit:focus, QComboBox:focus, QDoubleSpinBox:focus { border: 1px solid #2F6FED; }
QComboBox::drop-down { border: none; width: 18px; }
QComboBox QAbstractItemView { border: 1px solid #D8DDE5; selection-background-color: #EAF1FF; selection-color: #1F2329; }
QPushButton {
    background: #FFFFFF; border: 1px solid #D8DDE5; border-radius: 6px;
    padding: 6px 14px; min-height: 20px;
}
QPushButton:hover { border-color: #B9C2CF; background: #FAFBFC; }
QPushButton:pressed { background: #F0F2F5; }
QPushButton#primary { background: #2F6FED; border: 1px solid #2F6FED; color: #FFFFFF; font-weight: 600; }
QPushButton#primary:hover { background: #2A63D4; border-color: #2A63D4; }
QPushButton#primary:disabled { background: #A9C1F5; border-color: #A9C1F5; }
QPushButton#chip { padding: 3px 10px; color: #4B5563; font-size: 11px; border-radius: 11px; }
QCheckBox { spacing: 6px; }
QCheckBox::indicator { width: 14px; height: 14px; border: 1px solid #C6CCD6; border-radius: 3px; background: #FFF; }
QCheckBox::indicator:checked { background: #2F6FED; border-color: #2F6FED; }
QProgressBar { border: none; background: #DFE3EA; border-radius: 4px; height: 8px; text-align: center; }
QProgressBar::chunk { background: #2F6FED; border-radius: 4px; }
QTextEdit#log { background: #FBFCFD; border: 1px solid #E4E7EC; border-radius: 8px;
    font-family: Consolas,"Microsoft YaHei UI"; font-size: 11px; color: #4B5563; }
QTableWidget { background: #FFF; border: 1px solid #E4E7EC; border-radius: 8px; gridline-color: #EFF1F4; }
QHeaderView::section { background: #F7F8FA; border: none; border-bottom: 1px solid #E4E7EC;
    padding: 6px; font-weight: 600; color: #4B5563; }
"""


class FitScrollArea(QScrollArea):
    """sizeHint 取内容高度：窗口够大时正好铺满不出现滚动条，窗口变小时自动收缩出滚动条"""

    def sizeHint(self):
        w = self.widget()
        if w is not None:
            h = w.sizeHint().height()
            if h > 0:
                return QSize(super().sizeHint().width(), h)
        return super().sizeHint()


# --------------------------------------------------------------------------- 后台线程
class Worker(QThread):
    log = Signal(str)
    progress = Signal(int, int, str)          # 已处理字节, 总字节, 说明
    preview_done = Signal(object, object)     # parts, 错误信息
    build_done = Signal(object)

    def __init__(self, opts: Z.Options, mode: str):
        super().__init__()
        self.opts = opts
        self.mode = mode
        self._cancel = False

    def cancel(self):
        self._cancel = True

    def run(self):
        try:
            if self.mode == "preview":
                parts, skipped = Z.plan(self.opts, lambda m: None)
                self.preview_done.emit(parts, skipped)
            else:
                res = Z.build(self.opts, self._on_progress, lambda: self._cancel)
                self.build_done.emit(res)
        except Exception as ex:
            if self.mode == "preview":
                self.preview_done.emit(None, str(ex))
            else:
                self.build_done.emit(Z.BuildResult(ok=False, message=str(ex)))

    def _on_progress(self, d: dict):
        ph = d.get("phase")
        if ph == "file":
            done, total = d.get("done_bytes", 0), d.get("bytes", 1)
            msg = "第%d/%d部分 · %s" % (d.get("part", 1), d.get("parts", 1), d.get("current", ""))
            self.progress.emit(done, total, msg)
        elif ph == "part_done":
            self.log.emit("  ✓ %s  %.1f MB  用时 %.0f 秒" % (
                d.get("filename", ""), d.get("size", 0) / Z.MB, d.get("elapsed", 0)))
        elif ph == "start":
            self.log.emit("→ 开始打包 %s（%s）" % (d.get("filename", ""), Z.human(d.get("size", 0))))


# --------------------------------------------------------------------------- 预览对话框
class PreviewDialog(QDialog):
    def __init__(self, parts, skipped, limit_bytes, parent=None):
        super().__init__(parent)
        self.setWindowTitle("分包方案预览")
        self.resize(760, 460)
        lay = QVBoxLayout(self)
        lay.setContentsMargins(16, 16, 16, 16)
        lay.setSpacing(10)

        total = sum(p.size for p in parts)
        head = QLabel("共 <b>%d</b> 个压缩包，原始体积 <b>%s</b>，单包上限 <b>%s</b>"
                      % (len(parts), Z.human(total), Z.human(limit_bytes)))
        head.setTextFormat(Qt.RichText)
        lay.addWidget(head)

        tb = QTableWidget(len(parts), 5)
        tb.setHorizontalHeaderLabels(["压缩包文件名", "子文件夹", "文件数", "原始体积", "序号范围"])
        tb.verticalHeader().setVisible(False)
        tb.setEditTriggers(QAbstractItemView.NoEditTriggers)
        tb.setSelectionBehavior(QAbstractItemView.SelectRows)
        hh = tb.horizontalHeader()
        hh.setSectionResizeMode(0, QHeaderView.Stretch)
        for c in range(1, 5):
            hh.setSectionResizeMode(c, QHeaderView.ResizeToContents)
        for r, p in enumerate(parts):
            tb.setItem(r, 0, QTableWidgetItem(p.filename))
            tb.setItem(r, 1, QTableWidgetItem(str(len([u for u in p.units if u.name]))))
            tb.setItem(r, 2, QTableWidgetItem(str(len(p.entries))))
            tb.setItem(r, 3, QTableWidgetItem(Z.human(p.size)))
            tb.setItem(r, 4, QTableWidgetItem(p.range_text))
            if p.size > limit_bytes:
                tb.item(r, 3).setForeground(QBrush(QColor("#D9480F")))
        rows = max(len(parts), 1)
        tb.setMinimumHeight(min(430, 34 + rows * 31))
        tb.setMaximumHeight(min(430, 34 + rows * 31))
        lay.addWidget(tb)
        self.resize(780, 210 + min(rows, 10) * 31)

        if skipped:
            warn = QLabel("⚠ 有 %d 个最外层散落文件按当前设置不打包" % len(skipped))
            warn.setStyleSheet("color:#D9480F;")
            lay.addWidget(warn)
        tip = QLabel("提示：红色体积表示该包超过上限——这是单个子文件夹本身就超限、为了保证它不被切碎而独占一包。")
        tip.setObjectName("hint")
        tip.setWordWrap(True)
        lay.addWidget(tip)

        row = QHBoxLayout()
        row.addStretch(1)
        btn = QPushButton("关闭")
        btn.clicked.connect(self.accept)
        row.addWidget(btn)
        lay.addLayout(row)


# --------------------------------------------------------------------------- 主窗口
class MainWindow(QMainWindow):
    def __init__(self):
        super().__init__()
        self.setWindowTitle(APP_TITLE)
        self.setMinimumSize(880, 640)
        self.resize(1000, 900)
        self.setAcceptDrops(True)
        self.worker: Worker | None = None
        self.last_parts = None
        self._auto_out = ""
        self._preview_timer = QTimer(self)
        self._preview_timer.setSingleShot(True)
        self._preview_timer.timeout.connect(lambda: self.refresh_preview(silent=True))

        self._build_ui()
        self.setWindowIcon(make_icon())
        self._on_src_changed(suggest_out=True)

    # ---------------------------------------------------------------- UI
    def _build_ui(self):
        root = QWidget(objectName="root")
        self.setCentralWidget(root)
        outer = QVBoxLayout(root)
        outer.setContentsMargins(18, 16, 18, 16)
        outer.setSpacing(12)

        # 标题
        head = QHBoxLayout()
        tcol = QVBoxLayout()
        tcol.setSpacing(2)
        t = QLabel(APP_TITLE, objectName="title")
        s = QLabel(APP_SUB, objectName="sub")
        tcol.addWidget(t)
        tcol.addWidget(s)
        head.addLayout(tcol)
        head.addStretch(1)
        outer.addLayout(head)

        # 设置区放进滚动容器：屏幕小或缩放 150% 时也能完整看到所有选项
        sv = QVBoxLayout()
        sv.setContentsMargins(0, 0, 0, 0)
        sv.setSpacing(10)

        # 卡片1：目录
        card1 = self._card("目录")
        g = card1.layout().itemAt(1).layout()
        self.ed_src = QLineEdit()
        self.ed_src.setPlaceholderText("要打包的大文件夹，例如 桌面\\项目资料\\2024 年度验收资料")
        self.ed_src.textChanged.connect(lambda: self._schedule_preview())
        btn_src = QPushButton("浏览…")
        btn_src.clicked.connect(self.pick_src)
        g.addWidget(QLabel("源文件夹"), 0, 0)
        g.addWidget(self.ed_src, 0, 1, 1, 2)
        g.addWidget(btn_src, 0, 3)

        self.ed_out = QLineEdit()
        self.ed_out.setPlaceholderText("压缩包输出到哪里，默认在源文件夹旁边新建")
        btn_out = QPushButton("浏览…")
        btn_out.clicked.connect(self.pick_out)
        g.addWidget(QLabel("输出到"), 1, 0)
        g.addWidget(self.ed_out, 1, 1, 1, 2)
        g.addWidget(btn_out, 1, 3)
        g.setColumnStretch(1, 1)
        sv.addWidget(card1)

        # 卡片2：切分规则
        card2 = self._card("切分规则")
        g2 = card2.layout().itemAt(1).layout()
        self.sp_limit = QDoubleSpinBox()
        self.sp_limit.setRange(1, 100000)
        self.sp_limit.setDecimals(0)
        self.sp_limit.setSingleStep(50)
        self.sp_limit.setValue(900)
        self.sp_limit.setSuffix(" MB")
        self.sp_limit.setFixedWidth(120)
        self.sp_limit.valueChanged.connect(lambda: self._schedule_preview())
        chips = QHBoxLayout()
        chips.setSpacing(6)
        for v in (90, 500, 900, 1000):
            b = QPushButton("%dMB" % v, objectName="chip")
            b.setCursor(Qt.PointingHandCursor)
            b.clicked.connect(lambda _=False, x=v: self.sp_limit.setValue(x))
            chips.addWidget(b)
        chips.addStretch(1)

        g2.addWidget(QLabel("每个包上限"), 0, 0)
        lrow = QHBoxLayout()
        lrow.setSpacing(8)
        lrow.addWidget(self.sp_limit)
        lrow.addLayout(chips)
        g2.addLayout(lrow, 0, 1, 1, 3)

        self.cb_split = QComboBox()
        for k, v in Z.SPLIT_MODES.items():
            self.cb_split.addItem(v, k)
        self.cb_split.currentIndexChanged.connect(lambda: self._schedule_preview())

        self.cb_sort = QComboBox()
        for k, v in Z.SORT_MODES.items():
            self.cb_sort.addItem(v, k)
        self.cb_sort.currentIndexChanged.connect(lambda: self._schedule_preview())

        g2.addWidget(QLabel("切分粒度"), 1, 0)
        g2.addWidget(self.cb_split, 1, 1)
        g2.addWidget(QLabel("打包顺序"), 1, 2)
        g2.addWidget(self.cb_sort, 1, 3)

        self.cb_loose = QComboBox()
        for k, v in Z.LOOSE_MODES.items():
            self.cb_loose.addItem(v, k)
        self.cb_loose.setCurrentIndex(0)
        self.cb_loose.currentIndexChanged.connect(lambda: self._schedule_preview())
        g2.addWidget(QLabel("最外层散落文件"), 2, 0)
        g2.addWidget(self.cb_loose, 2, 1)
        hint = QLabel("源文件夹最外层、不属于任何子文件夹的文件（如报价表），默认放进第 1 部分")
        hint.setObjectName("hint")
        hint.setWordWrap(True)
        g2.addWidget(hint, 2, 2, 1, 2)
        g2.setColumnStretch(1, 3)
        g2.setColumnStretch(3, 3)
        sv.addWidget(card2)

        # 卡片3：命名与压缩
        card3 = self._card("命名与压缩")
        g3 = card3.layout().itemAt(1).layout()
        self.ed_prefix = QLineEdit()
        self.ed_prefix.setPlaceholderText("留空则用源文件夹名")
        self.ed_prefix.textChanged.connect(lambda: self._schedule_preview())
        self.cb_label = QComboBox()
        self.cb_label.setEditable(True)
        self.cb_label.addItems(["部分", "批", "卷", "PART"])
        self.cb_label.currentTextChanged.connect(lambda: self._schedule_preview())
        self.cb_label.setFixedWidth(110)
        self.ck_range = QCheckBox("文件名带序号范围")
        self.ck_range.setChecked(True)
        self.ck_range.toggled.connect(lambda: self._schedule_preview())

        self.cb_compress = QComboBox()
        for k, v in Z.COMPRESS_MODES.items():
            self.cb_compress.addItem(v, k)

        g3.addWidget(QLabel("文件名前缀"), 0, 0)
        g3.addWidget(self.ed_prefix, 0, 1, 1, 3)

        g3.addWidget(QLabel("部分标记"), 1, 0)
        g3.addWidget(self.cb_label, 1, 1, Qt.AlignLeft)
        g3.addWidget(self.ck_range, 1, 2, 1, 2, Qt.AlignLeft)

        g3.addWidget(QLabel("压缩方式"), 2, 0)
        g3.addWidget(self.cb_compress, 2, 1, 1, 3)

        self.lbl_sample = QLabel("")
        self.lbl_sample.setObjectName("big")
        self.lbl_sample.setContentsMargins(0, 4, 0, 0)
        g3.addWidget(self.lbl_sample, 3, 0, 1, 4)
        g3.setColumnStretch(1, 2)
        g3.setColumnStretch(2, 1)
        g3.setVerticalSpacing(10)
        sv.addWidget(card3)

        # 卡片4：其它
        card4 = self._card("其它选项")
        g4 = card4.layout().itemAt(1).layout()
        self.ck_manifest = QCheckBox("输出目录生成总清单 txt")
        self.ck_manifest.setChecked(True)
        self.ck_inside = QCheckBox("每个包内附一份本包清单")
        self.ck_empty = QCheckBox("保留空文件夹")
        self.ck_empty.setChecked(True)
        self.ck_hidden = QCheckBox("包含隐藏文件")
        self.ck_open = QCheckBox("完成后打开输出目录")
        self.ck_open.setChecked(True)
        self.ck_over = QCheckBox("覆盖同名压缩包")
        self.ck_over.setChecked(True)
        g4.addWidget(self.ck_manifest, 0, 0)
        g4.addWidget(self.ck_inside, 0, 1)
        g4.addWidget(self.ck_empty, 0, 2)
        g4.addWidget(self.ck_hidden, 1, 0)
        g4.addWidget(self.ck_open, 1, 1)
        g4.addWidget(self.ck_over, 1, 2)
        sv.addWidget(card4)
        sv.addStretch(1)
        holder = QWidget()
        holder.setLayout(sv)
        scroll = FitScrollArea()
        scroll.setWidget(holder)
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        scroll.setStyleSheet("QScrollArea{background:transparent;} QWidget#qt_scrollarea_viewport{background:transparent;}")
        # 高度跟随内容：够高时不出现滚动条，窗口拉小时自动滚动
        outer.addWidget(scroll, 0)

        # 操作区
        act = QHBoxLayout()
        act.setSpacing(10)
        self.lbl_est = QLabel("等待选择源文件夹…")
        self.lbl_est.setObjectName("hint")
        act.addWidget(self.lbl_est, 1)

        self.btn_preview = QPushButton("预览方案")
        self.btn_preview.clicked.connect(lambda: self.refresh_preview(silent=False))
        self.btn_start = QPushButton("开始打包", objectName="primary")
        self.btn_start.setMinimumWidth(120)
        self.btn_start.setDefault(True)
        self.btn_start.clicked.connect(self.start_build)
        self.btn_cancel = QPushButton("取消")
        self.btn_cancel.setEnabled(False)
        self.btn_cancel.clicked.connect(self.cancel_build)
        act.addWidget(self.btn_preview)
        act.addWidget(self.btn_cancel)
        act.addWidget(self.btn_start)
        outer.addLayout(act)

        self.pb = QProgressBar()
        self.pb.setTextVisible(False)
        self.pb.setFixedHeight(8)
        self.pb.setValue(0)
        outer.addWidget(self.pb)

        self.log = QTextEdit(objectName="log")
        self.log.setReadOnly(True)
        self.log.setMinimumHeight(90)
        self.log.setPlaceholderText("打包日志会显示在这里：每个包的体积、耗时、跳过的文件……")
        outer.addWidget(self.log, 1)

    def _card(self, title: str) -> QFrame:
        card = QFrame(objectName="card")
        # 竖向固定：卡片高度永远等于 sizeHint，窗口再矮也不会把里面的控件压重叠
        card.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        v = QVBoxLayout(card)
        v.setContentsMargins(14, 12, 14, 12)
        v.setSpacing(10)
        lab = QLabel(title, objectName="cardTitle")
        v.addWidget(lab)
        g = QGridLayout()
        g.setHorizontalSpacing(10)
        g.setVerticalSpacing(8)
        v.addLayout(g)
        return card

    # ---------------------------------------------------------------- 交互
    def pick_src(self):
        d = QFileDialog.getExistingDirectory(self, "选择要打包的文件夹", self.ed_src.text() or os.path.expanduser("~\\Desktop"))
        if d:
            self.ed_src.setText(os.path.normpath(d))
            auto = os.path.join(os.path.dirname(os.path.normpath(d)), "分包结果")
            if not self.ed_out.text().strip() or self.ed_out.text().strip() == self._auto_out:
                self._auto_out = auto
                self.ed_out.setText(auto)

    def pick_out(self):
        d = QFileDialog.getExistingDirectory(self, "选择输出文件夹", self.ed_out.text() or os.path.expanduser("~\\Desktop"))
        if d:
            self.ed_out.setText(os.path.normpath(d))

    def dragEnterEvent(self, e):
        if e.mimeData().hasUrls():
            for u in e.mimeData().urls():
                if os.path.isdir(u.toLocalFile()):
                    e.acceptProposedAction()
                    return
        e.ignore()

    def dropEvent(self, e):
        for u in e.mimeData().urls():
            p = u.toLocalFile()
            if os.path.isdir(p):
                self.ed_src.setText(os.path.normpath(p))
                break

    def _on_src_changed(self, suggest_out=False):
        src = self.ed_src.text().strip()
        if src and suggest_out and not self.ed_out.text().strip():
            self.ed_out.setText(os.path.join(os.path.dirname(os.path.normpath(src)), "分包结果"))

    def _schedule_preview(self):
        self._on_src_changed()
        self._update_sample()
        if self.ed_src.text().strip() and os.path.isdir(self.ed_src.text().strip()):
            self._preview_timer.start(600)
        else:
            self.lbl_est.setText("等待选择源文件夹…")

    def _update_sample(self):
        opts = self.collect(require_src=False)
        tag = Z.part_tag(opts, 1)
        rng = "01-22" if opts.include_range else None
        sample = "%s_%s%s.zip" % (opts.eff_prefix or "前缀", tag, ("_" + rng) if rng else "")
        self.lbl_sample.setText(
            '<span style="color:#8A9099;font-weight:400">文件名示例：</span>'
            '<span style="color:#2F6FED;font-weight:600">%s</span>' % sample)
        self.lbl_sample.setTextFormat(Qt.RichText)

    def collect(self, require_src=True) -> Z.Options:
        label = self.cb_label.currentText().strip() or "部分"
        return Z.Options(
            src=self.ed_src.text().strip(),
            out=self.ed_out.text().strip(),
            limit_mb=float(self.sp_limit.value()),
            sort_mode=self.cb_sort.currentData(),
            split_mode=self.cb_split.currentData(),
            loose_mode=self.cb_loose.currentData(),
            label=label,
            prefix=self.ed_prefix.text().strip(),
            include_range=self.ck_range.isChecked(),
            compress=self.cb_compress.currentData(),
            include_hidden=self.ck_hidden.isChecked(),
            keep_empty_dirs=self.ck_empty.isChecked(),
            make_manifest=self.ck_manifest.isChecked(),
            manifest_inside=self.ck_inside.isChecked(),
            overwrite=self.ck_over.isChecked(),
            open_when_done=self.ck_open.isChecked(),
        )

    def validate(self):
        opts = self.collect()
        if not opts.src:
            QMessageBox.warning(self, "提示", "请先选择源文件夹")
            return None
        if not os.path.isdir(opts.src):
            QMessageBox.warning(self, "提示", "源文件夹不存在：\n%s" % opts.src)
            return None
        if not opts.out:
            opts.out = os.path.join(os.path.dirname(os.path.normpath(opts.src)), "分包结果")
            self.ed_out.setText(opts.out)
        if os.path.normpath(opts.out) == os.path.normpath(opts.src):
            QMessageBox.warning(self, "提示", "输出目录不能和源文件夹相同，请另选一个")
            return None
        return opts

    # ---------------------------------------------------------------- 预览
    def refresh_preview(self, silent=True):
        if self.worker and self.worker.isRunning():
            return
        opts = self.validate()
        if not opts:
            return
        self.lbl_est.setText("正在统计体积…")
        self.worker = Worker(opts, "preview")
        self.worker.preview_done.connect(lambda parts, err: self._on_preview(parts, err, opts, silent))
        self.worker.start()

    def _on_preview(self, parts, err, opts, silent):
        if parts is None:
            self.lbl_est.setText("统计失败：%s" % err)
            if not silent:
                QMessageBox.warning(self, "无法预览", str(err))
            return
        total = sum(p.size for p in parts)
        biggest = max((p.size for p in parts), default=0)
        self.lbl_est.setText("预计 %d 个包 · 合计 %s · 最大包 %s" % (len(parts), Z.human(total), Z.human(biggest)))
        self.last_parts = (parts, opts)
        if not silent:
            PreviewDialog(parts, getattr(self, "_skipped", []), opts.limit_bytes, self).exec()

    # ---------------------------------------------------------------- 打包
    def start_build(self):
        if self.worker and self.worker.isRunning():
            return
        opts = self.validate()
        if not opts:
            return
        if os.path.isdir(opts.out) and os.listdir(opts.out) and not opts.overwrite:
            QMessageBox.warning(self, "提示", "输出目录已有文件，且未勾选“覆盖同名压缩包”")
            return
        self._preview_timer.stop()
        self.log.clear()
        self.pb.setValue(0)
        self._set_running(True)
        self.log.append("源文件夹：%s" % opts.src)
        self.log.append("输出到：  %s" % opts.out)
        self.log.append("单包上限：%s    切分：%s    顺序：%s" % (
            Z.human(opts.limit_bytes), Z.SPLIT_MODES[opts.split_mode], Z.SORT_MODES[opts.sort_mode]))
        self.log.append("—" * 60)
        self.worker = Worker(opts, "build")
        self.worker.progress.connect(self._on_progress)
        self.worker.log.connect(lambda s: self.log.append(s))
        self.worker.build_done.connect(lambda r: self._on_built(r, opts))
        self.worker.start()

    def cancel_build(self):
        if self.worker and self.worker.isRunning():
            self.worker.cancel()
            self.log.append("正在取消…")

    def _on_progress(self, done, total, msg):
        self.pb.setMaximum(max(total, 1))
        self.pb.setValue(min(done, total))
        self.lbl_est.setText("%s  %s / %s" % (msg, Z.human(done), Z.human(total)))

    def _on_built(self, res: Z.BuildResult, opts: Z.Options):
        self._preview_timer.stop()
        self._set_running(False)
        if not res.ok:
            self.log.append("✗ " + res.message)
            if "取消" in res.message:
                self.lbl_est.setText("已取消")
            else:
                QMessageBox.critical(self, "打包失败", res.message)
            return
        self.pb.setValue(self.pb.maximum())
        total = sum(s for _, _, s in res.parts)
        self.log.append("—" * 60)
        self.log.append("✓ %s，共 %d 个包，合计 %s" % (res.message, len(res.parts), Z.human(total)))
        if res.manifest:
            self.log.append("  清单：%s" % os.path.basename(res.manifest))
        if res.skipped:
            self.log.append("  ⚠ 跳过 %d 个文件（可能被占用）：%s" % (len(res.skipped), res.skipped[0]))
        self.lbl_est.setText("完成：%d 个包，合计 %s" % (len(res.parts), Z.human(total)))
        if opts.open_when_done:
            try:
                os.startfile(opts.out)
            except Exception:
                pass

    def _set_running(self, running: bool):
        for w in (self.btn_start, self.btn_preview):
            w.setEnabled(not running)
        self.btn_cancel.setEnabled(running)
        self.btn_start.setText("打包中…" if running else "开始打包")


def make_icon() -> QIcon:
    pm = QPixmap(64, 64)
    pm.fill(Qt.transparent)
    p = QPainter(pm)
    p.setRenderHint(QPainter.Antialiasing)
    p.setBrush(QBrush(QColor(ACCENT)))
    p.setPen(Qt.NoPen)
    p.drawRoundedRect(2, 2, 60, 60, 14, 14)
    p.setPen(QPen(QColor("#FFFFFF"), 5, Qt.SolidLine, Qt.RoundCap))
    # 三个堆叠的“包”
    p.drawLine(18, 24, 46, 24)
    p.drawLine(18, 34, 46, 34)
    p.drawLine(18, 44, 38, 44)
    p.end()
    return QIcon(pm)


def selftest() -> int:
    """--selftest：不显示窗口，跑一遍界面构建 + 打包，用于验证打包后的 exe 是否完整"""
    import tempfile
    import shutil
    log_path = os.path.join(tempfile.gettempdir(), "分包打包器_selftest.log")
    lines = []
    try:
        root = os.path.join(tempfile.gettempdir(), "_zippack_selftest")
        src = os.path.join(root, "样例")
        out = os.path.join(root, "out")
        for d in (src, out):
            if os.path.isdir(d):
                shutil.rmtree(d)
        os.makedirs(src)
        for i, mb in ((1, 3), (2, 1), (10, 2)):
            d = os.path.join(src, "%d、样例%d" % (i, i))
            os.makedirs(d)
            with open(os.path.join(d, "a.jpg"), "wb") as f:
                f.write(os.urandom(mb * 1024 * 1024))
        with open(os.path.join(src, "最外层清单.xlsx"), "wb") as f:
            f.write(b"PK" + os.urandom(2048))

        app = QApplication(sys.argv[:1])
        app.setStyleSheet(QSS)
        w = MainWindow()            # 只构建，不 show
        w.ed_src.setText(src)
        opts = w.collect()
        opts.out = out
        opts.limit_mb = 4
        res = Z.build(opts, lambda d: None)
        lines.append("ok=%s" % res.ok)
        lines.append("message=%s" % res.message)
        for fn, n, sz in res.parts:
            lines.append("%s | %d files | %d bytes" % (fn, n, sz))
        lines.append("widgets=%d" % len(w.findChildren(QWidget)))
        ok = res.ok and len(res.parts) >= 2
    except Exception:
        lines.append("EXCEPTION:\n" + traceback.format_exc())
        ok = False
    with open(log_path, "w", encoding="utf-8") as f:
        f.write("\n".join(lines))
    return 0 if ok else 1


def main():
    if "--selftest" in sys.argv:
        sys.exit(selftest())
    app = QApplication(sys.argv)
    app.setApplicationName(APP_TITLE)
    app.setStyle("Fusion")
    app.setStyleSheet(QSS)
    f = QFont("Microsoft YaHei UI", 9)
    app.setFont(f)
    w = MainWindow()
    w.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()

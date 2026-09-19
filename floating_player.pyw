"""Small native Qt companion. JRiver owns every audio sample and playback queue."""
from __future__ import annotations

import json
import math
import ctypes
from ctypes import wintypes
import re
import logging
from logging.handlers import RotatingFileHandler
from pathlib import Path
import sys
import time

from PySide6.QtCore import Qt, QTimer, Signal, Slot, QObject, QRunnable, QThreadPool, QPoint, QRectF, QEvent
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen, QPixmap, QIcon, QFont, QDesktopServices, QActionGroup
from PySide6.QtCore import QUrl
from PySide6.QtNetwork import QLocalServer, QLocalSocket
from PySide6.QtWidgets import (QApplication, QWidget, QFrame, QVBoxLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QSystemTrayIcon, QMenu, QFileDialog, QSizePolicy, QBoxLayout,
    QDialog, QListWidget, QListWidgetItem, QTextBrowser, QAbstractItemView)

from floating_bridge import Bridge, DATA, lyric_index, import_lyrics, parse_lrc
from cloud_lyrics import CloudLyrics
from input_modes import GlobalModeHotkey, set_input_mode
from settings import SETTINGS

CONFIG = DATA / 'window.json'


class Result(QObject):
    done = Signal(int, object, object)


class Work(QRunnable):
    def __init__(self, fn, token, result):
        super().__init__()
        self.fn = fn
        self.token = token
        self.result = result

    def run(self):
        try:
            value, error = self.fn(), None
        except Exception as exc:
            value, error = None, str(exc)
        self.result.done.emit(self.token, value, error)


def icon(kind, color='#dce5ee'):
    pix = QPixmap(32, 32)
    pix.fill(Qt.transparent)
    p = QPainter(pix)
    p.setRenderHint(QPainter.Antialiasing)
    p.setPen(QPen(QColor(color), 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
    if kind == 'play':
        p.setBrush(QColor(color)); path = QPainterPath()
        path.moveTo(12, 8); path.lineTo(24, 16); path.lineTo(12, 24); path.closeSubpath(); p.drawPath(path)
    elif kind == 'pause':
        p.setBrush(QColor(color)); p.drawRoundedRect(QRectF(10, 8, 4, 16), 1, 1); p.drawRoundedRect(QRectF(19, 8, 4, 16), 1, 1)
    elif kind in ('next', 'previous'):
        if kind == 'previous':
            p.translate(32, 0); p.scale(-1, 1)
        path = QPainterPath(); path.moveTo(9, 9); path.lineTo(20, 16); path.lineTo(9, 23); path.closeSubpath()
        p.setBrush(QColor(color)); p.drawPath(path); p.drawLine(23, 9, 23, 23)
    elif kind in ('expand', 'collapse'):
        a, b = (13, 19) if kind == 'expand' else (19, 13)
        p.drawLine(10, a, 16, b); p.drawLine(16, b, 22, a)
    elif kind == 'close':
        p.drawLine(11, 11, 21, 21); p.drawLine(21, 11, 11, 21)
    elif kind == 'minimize':
        p.drawLine(10, 19, 22, 19)
    elif kind == 'pin':
        path = QPainterPath(); path.moveTo(12, 8); path.lineTo(20, 8); path.lineTo(19, 15)
        path.lineTo(23, 19); path.lineTo(9, 19); path.lineTo(13, 15); path.closeSubpath(); p.drawPath(path); p.drawLine(16, 19, 16, 26)
    elif kind == 'music':
        p.drawLine(14, 9, 24, 7); p.drawLine(14, 9, 14, 23); p.drawLine(24, 7, 24, 20)
        p.setBrush(QColor(color)); p.drawEllipse(QRectF(7, 20, 7, 5)); p.drawEllipse(QRectF(17, 17, 7, 5))
    p.end()
    return QIcon(pix)


class ElideLabel(QLabel):
    def __init__(self, text='', parent=None):
        super().__init__(parent)
        self.full = text
        self.offset = 0
        self.scroll_timer = QTimer(self)
        self.scroll_timer.timeout.connect(self.scroll_text)
        self.scroll_timer.start(70)
        self.setTextFormat(Qt.PlainText)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        self.setText(text)

    def setText(self, text):
        if str(text) != self.full:
            self.offset = -25
        self.full = str(text)
        self.setToolTip(self.full)
        self.refresh()

    def refresh(self):
        super().setText('')
        self.setMinimumHeight(self.fontMetrics().height())
        self.update()

    def resizeEvent(self, event):
        self.refresh()
        super().resizeEvent(event)

    def scroll_text(self):
        if self.isVisible() and self.fontMetrics().horizontalAdvance(self.full) > self.width():
            self.offset += 1
            if self.offset > self.fontMetrics().horizontalAdvance(self.full) + 30:
                self.offset = -25
            self.update()

    def paintEvent(self, event):
        p = QPainter(self)
        p.setFont(self.font()); p.setPen(self.palette().color(self.foregroundRole()))
        p.setClipRect(self.rect())
        offset = max(0, self.offset) if self.fontMetrics().horizontalAdvance(self.full) > self.width() else 0
        p.drawText(-offset, (self.height() + self.fontMetrics().ascent() - self.fontMetrics().descent()) // 2, self.full)
        p.end()


class ClampedLabel(QLabel):
    def __init__(self, text='', **kwargs):
        super().__init__('', **kwargs)
        self.full = text
        self.max_lines = None
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Ignored)

    def setText(self, text):
        self.full = str(text); self.refresh()

    def refresh(self):
        from PySide6.QtCore import QRect
        fm = self.fontMetrics()
        if not self.wordWrap():
            text = fm.elidedText(self.full.replace('\n', ' '), Qt.ElideRight, max(1, self.width()))
        else:
            text = self.full
            rect = QRect(0, 0, max(1, self.width()), 100000)
            available = min(self.height(), fm.lineSpacing() * self.max_lines) if self.max_lines else self.height()
            if fm.boundingRect(rect, Qt.TextWordWrap, text).height() > available:
                low, high = 0, len(text)
                while low < high:
                    mid = (low + high + 1) // 2
                    if fm.boundingRect(rect, Qt.TextWordWrap, text[:mid]+'…').height() <= available: low = mid
                    else: high = mid - 1
                text = text[:low].rstrip() + '…'
        super().setText(text)

    def resizeEvent(self, event):
        self.refresh(); super().resizeEvent(event)

    def changeEvent(self, event):
        super().changeEvent(event)
        if event.type() == QEvent.FontChange and hasattr(self, 'full'):
            self.refresh()


class SeekSlider(QSlider):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.setSliderDown(True)
            self.setValue(round(self.maximum() * max(0, min(1, event.position().x() / max(1, self.width())))))
            event.accept()
        else:
            super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if self.isSliderDown():
            self.setValue(round(self.maximum() * max(0, min(1, event.position().x() / max(1, self.width())))))
        else:
            super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if self.isSliderDown():
            self.setSliderDown(False)
            event.accept()
        else:
            super().mouseReleaseEvent(event)


class ReviewReader(QTextBrowser):
    """Complete, scrollable text; repeated metadata refreshes preserve reading position."""
    def __init__(self, text='', **kwargs):
        super().__init__(**kwargs)
        self.full = ''
        self.setReadOnly(True)
        self.setOpenLinks(False)
        self.setFrameShape(QFrame.NoFrame)
        self.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setVerticalScrollBarPolicy(Qt.ScrollBarAsNeeded)
        self.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.document().setDocumentMargin(0)
        self.setText(text)

    def setText(self, text):
        text = str(text)
        if text != self.full:
            self.full = text
            self.setPlainText(text)
            self.verticalScrollBar().setValue(0)

    def text(self):
        return self.full

    def refresh(self):
        self.viewport().update()


class ReviewResizeHandle(QWidget):
    def __init__(self, owner, edges):
        super().__init__(owner)
        self.edges = edges
        self.setCursor(Qt.SizeFDiagCursor if edges & Qt.RightEdge else Qt.SizeVerCursor)
        area = '曲评' if owner.reviews_enabled else '歌词'
        self.setToolTip('拖动调整宽度和高度' if edges & Qt.RightEdge else '向下拖动，增加'+area+'空间')
        self.setAccessibleName('调整窗口宽高' if edges & Qt.RightEdge else '拉长'+area+'区域')

    def mousePressEvent(self, event):
        owner = self.window()
        if event.button() == Qt.LeftButton and not owner.click_through:
            owner.windowHandle().startSystemResize(self.edges)
            event.accept()

    def paintEvent(self, event):
        p = QPainter(self); p.setRenderHint(QPainter.Antialiasing)
        p.setPen(QPen(QColor('#778b98'), 1.2, Qt.SolidLine, Qt.RoundCap))
        if self.edges & Qt.RightEdge:
            p.drawLine(4, self.height()-4, self.width()-4, 4)
            p.drawLine(9, self.height()-4, self.width()-4, 9)
        else:
            mid = self.width()//2
            p.drawLine(mid-16, self.height()//2, mid+16, self.height()//2)
        p.end()


class DragHeader(QFrame):
    def mousePressEvent(self, event):
        if event.button() == Qt.LeftButton:
            self.window().windowHandle().startSystemMove()

    def mouseDoubleClickEvent(self, event):
        self.window().toggle_expanded()


class Player(QWidget):
    def __init__(self, bridge=None):
        super().__init__()
        self.bridge = bridge or Bridge()
        self.pool = QThreadPool(self)
        self.pool.setMaxThreadCount(4)
        self.results = Result(self)
        self.results.done.connect(self.work_done, Qt.QueuedConnection)
        self.callbacks = {}
        self.next_job = 0
        self.setWindowTitle('JRiver 浮窗')
        self.setWindowIcon(icon('music', '#a9cfbb'))
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.setWindowFlags(Qt.FramelessWindowHint | Qt.Window | Qt.WindowStaysOnTopHint)
        self.config = {}
        try:
            self.config = json.loads(CONFIG.read_text(encoding='utf-8'))
        except (OSError, ValueError):
            pass
        raw_sizes = self.config.get('custom_sizes', {})
        self.custom_sizes = {k: v for k, v in raw_sizes.items() if isinstance(k, str) and
                             isinstance(v, list) and len(v) == 2 and all(type(n) is int and 0 < n < 10000 for n in v)} if isinstance(raw_sizes, dict) else {}
        self.native_resizing = False
        self.restore_size_pending = None
        self.review_full_text = ''
        self.reviews_enabled = bool(SETTINGS.get('reviews_file'))
        self.expanded = bool(self.config.get('expanded', True))
        self.mode = self.config.get('mode', 'full' if self.expanded else 'compact')
        if self.mode not in ('full', 'compact', 'mini'): self.mode = 'full'
        self.ui_scale = self.config.get('scale', .75)
        if self.ui_scale not in (.5, .75, 1, 1.25): self.ui_scale = .75
        self.opacity = self.config.get('opacity', 100)
        if self.opacity not in (40, 60, 80, 90, 100): self.opacity = 100
        self.hover_clear = bool(self.config.get('hover_clear', True))
        self.click_through = bool(self.config.get('click_through', False))
        self.setAttribute(Qt.WA_ShowWithoutActivating, self.click_through)
        self.input_ready = False
        self.closing = False
        self.cloud = CloudLyrics(DATA)
        self.cloud_busy = False
        self.cloud_attempts = set()
        self.cover_original = QPixmap()
        self.pinned = bool(self.config.get('pinned', True))
        self.key = None
        self.track = {}
        self.info = {}
        self.lines = []
        self.poll_busy = False
        self.control_busy = False
        self.detail_busy = False
        self.connected = False
        self.lyric_row = None
        self.last_position = 0
        self.last_tick = time.monotonic()
        self.last_details = 0
        self.message_until = 0
        self.seek_context = None
        self.last_rating_change = None
        self.rating_verified = None
        self.build()
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.pinned)
        self.capture_metrics()
        self.apply_mode()
        self.update_pin()
        self.restore_position()
        self.save_timer = QTimer(self)
        self.save_timer.setSingleShot(True)
        self.save_timer.timeout.connect(self.save)
        self.poll_timer = QTimer(self)
        self.poll_timer.timeout.connect(self.poll)
        self.poll_timer.start(700)
        self.tick_timer = QTimer(self)
        self.tick_timer.timeout.connect(self.tick)
        self.tick_timer.start(100)
        self.setup_tray()
        self.mode_hotkey = GlobalModeHotkey(self.toggle_input_mode)
        if not self.mode_hotkey.register():
            logging.warning('Global mode hotkey unavailable: %s', self.mode_hotkey.last_error)
        else:
            logging.info('Global input-mode hotkey registered: %s', self.mode_hotkey.label)
        self.input_ready = True
        self.update_input_ui()
        self.poll()

    def button(self, kind, tip, callback, size=28):
        b = QPushButton()
        b.setObjectName(kind)
        b.setAccessibleName(tip)
        b.setToolTip(tip)
        b.setIcon(icon(kind))
        b.setFixedSize(size, size)
        b.setCursor(Qt.PointingHandCursor)
        b.clicked.connect(callback)
        return b

    def build(self):
        self.base_style = '''
            QWidget { color: #dbe4ec; font-family: "Microsoft YaHei UI"; font-size: 12px; }
            QFrame#surface { background: rgb(19, 25, 32); border: 1px solid #3a4249; border-radius: 18px; }
            QLabel { background: transparent; border: none; }
            QLabel#brand { color: #9db5aa; font-size: 10px; font-weight: 600; letter-spacing: 2px; }
            QLabel#title { color: #f0f3f5; font-size: 17px; font-weight: 600; }
            QLabel#artist { color: #a3afb9; font-size: 12px; }
            QLabel#time, QLabel#status { color: #7f939e; font-size: 10px; }
            QPushButton { border: none; background: transparent; border-radius: 7px; padding: 0; }
            QPushButton:hover { background: #35434b; }
            QPushButton:pressed { background: #465b60; }
            QPushButton:disabled { color: #52606b; }
            QPushButton#play { background: #b8d7c4; border-radius: 19px; }
            QPushButton#play:hover { background: #d2e8da; }
            QPushButton#star { color: #deb980; font-size: 21px; }
            QPushButton#undo_rating { color: #9bb6aa; font-size: 18px; }
            QPushButton#text { color: #9bb6aa; font-size: 10px; padding: 3px 7px; }
            QSlider::groove:horizontal { height: 3px; background: #3b464f; border-radius: 1px; }
            QSlider::sub-page:horizontal { background: #afd0bd; }
            QSlider::handle:horizontal { background: #d3e9dc; width: 9px; height: 9px; margin: -3px 0; border-radius: 4px; }
            QFrame#lyrics { background: rgba(44, 56, 65, 95); border-radius: 10px; }
            QFrame#review { background: rgba(63, 73, 77, 65); border-radius: 10px; }
            QLabel#lyricCurrent { color: #c4e3cf; font-size: 16px; font-weight: 500; }
            QLabel#lyricSide { color: #819099; font-size: 11px; }
            QLabel#section { color: #94aba0; font-size: 10px; }
            QLabel#reviewText { color: rgba(206, 217, 222, 185); font-size: 11px; }
            QTextBrowser#reviewText { color: #ced9de; background: transparent; border: none; font-size: 11px; padding: 0; }
            QLabel#reviewScore { color: #deb980; font-size: 11px; font-weight: 600; }
            QScrollBar:vertical { background: transparent; width: 6px; margin: 0; }
            QScrollBar::handle:vertical { background: #647b85; min-height: 18px; border-radius: 3px; }
            QScrollBar::add-line:vertical, QScrollBar::sub-line:vertical { height: 0; }
            QScrollBar::add-page:vertical, QScrollBar::sub-page:vertical { background: transparent; }
            QMenu { background: #1c252d; border: 1px solid #48565c; padding: 6px; }
            QMenu::item { padding: 7px 14px; } QMenu::item:selected { background: #35434b; }
        '''
        self.setStyleSheet(self.base_style)
        outer = QVBoxLayout(self)
        outer.setContentsMargins(5, 5, 5, 5)
        surface = QFrame(objectName='surface')
        outer.addWidget(surface)
        main = QVBoxLayout(surface)
        self.main_layout = main
        main.setContentsMargins(17, 9, 17, 14)
        main.setSpacing(8)
        header = DragHeader()
        self.header = header
        header.setFixedHeight(26)
        h = QHBoxLayout(header); h.setContentsMargins(0, 0, 0, 0); h.setSpacing(3)
        self.brand = QLabel('JRIVER', objectName='brand'); h.addWidget(self.brand)
        h.addStretch()
        self.pin = self.button('pin', '置顶开关', self.toggle_pin)
        self.expand = self.button('collapse', '展开 / 收起歌词和曲评', self.toggle_expanded)
        self.settings_button = QPushButton('•••'); self.settings_button.setFixedSize(28, 28)
        self.settings_button.setToolTip('窗口模式、缩放与透明度'); self.settings_button.clicked.connect(self.show_settings)
        h.addWidget(self.settings_button)
        h.addWidget(self.pin); h.addWidget(self.expand)
        h.addWidget(self.button('minimize', '收起到托盘', self.hide))
        h.addWidget(self.button('close', '关闭浮窗（JRiver 继续播放）', self.close))
        main.addWidget(header)
        summary = QHBoxLayout(); summary.setSpacing(14)
        self.summary_layout = summary
        self.cover = QLabel('♫'); self.cover.setAlignment(Qt.AlignCenter)
        self.cover.setFixedSize(82, 82); self.cover.setStyleSheet('background:#2b3942;border-radius:10px;color:#9fb8ab;font-size:30px;')
        self.cover.setAccessibleName('当前专辑封面')
        summary.addWidget(self.cover)
        meta = QVBoxLayout(); meta.setSpacing(2)
        self.title = ElideLabel('等待 JRiver…'); self.title.setObjectName('title')
        self.artist = ElideLabel('连接本机 Media Center 36'); self.artist.setObjectName('artist')
        meta.addWidget(self.title); meta.addWidget(self.artist)
        self.album = ElideLabel(''); self.album.setObjectName('status'); meta.addWidget(self.album)
        stars = QHBoxLayout(); stars.setSpacing(0)
        self.stars = []
        for i in range(1, 6):
            b = QPushButton('☆', objectName='star'); b.setFixedSize(26, 26)
            b.setToolTip(f'将当前曲目设为 {i} 星'); b.setAccessibleName(f'{i} 星')
            b.clicked.connect(lambda checked=False, rating=i: self.command('rating', value=rating))
            b.setContextMenuPolicy(Qt.CustomContextMenu)
            b.customContextMenuRequested.connect(lambda pos: self.show_rating_menu())
            self.stars.append(b); stars.addWidget(b)
        self.undo_rating_button = QPushButton('↶', objectName='undo_rating')
        self.undo_rating_button.setFixedSize(24, 24); self.undo_rating_button.setToolTip('撤销上次评分')
        self.undo_rating_button.setAccessibleName('撤销上次评分')
        self.undo_rating_button.clicked.connect(self.undo_rating)
        self.undo_rating_button.setEnabled(False); stars.addWidget(self.undo_rating_button)
        stars.addStretch()
        self.state = QLabel('连接中', objectName='status'); stars.addWidget(self.state)
        meta.addLayout(stars); summary.addLayout(meta, 1)
        main.addLayout(summary)
        self.control_host = QWidget()
        self.control_host.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Fixed)
        controls = QHBoxLayout(self.control_host); controls.setContentsMargins(0,0,0,0); controls.setSpacing(8)
        self.controls_layout = controls
        transport = QWidget(); transport_layout = QHBoxLayout(transport); transport_layout.setContentsMargins(0,0,0,0); transport_layout.setSpacing(8)
        self.previous = self.button('previous', '上一曲', lambda: self.command('Previous'), 30)
        self.play = self.button('play', '播放 / 暂停', self.play_pause, 38)
        self.play.setIcon(icon('play', '#21362a'))
        self.next = self.button('next', '下一曲', lambda: self.command('Next'), 30)
        transport_layout.addWidget(self.previous); transport_layout.addWidget(self.play); transport_layout.addWidget(self.next)
        controls.addWidget(transport)
        timeline = QWidget(); timeline_layout = QHBoxLayout(timeline); timeline_layout.setContentsMargins(0,0,0,0); timeline_layout.setSpacing(8)
        self.elapsed = QLabel('0:00', objectName='time'); self.elapsed.setFixedWidth(34)
        self.seek = SeekSlider(Qt.Horizontal)
        self.seek.setAccessibleName('播放进度')
        self.seek.setRange(0, 1)
        self.seek.sliderPressed.connect(lambda: setattr(self, 'seek_context', (self.key, self.info.get('ZoneID', '-1'))))
        self.seek.sliderReleased.connect(self.seek_to)
        self.total = QLabel('0:00', objectName='time'); self.total.setFixedWidth(34)
        timeline_layout.addWidget(self.elapsed); timeline_layout.addWidget(self.seek, 1); timeline_layout.addWidget(self.total)
        controls.addWidget(timeline, 1)
        main.addWidget(self.control_host)
        self.details = QWidget()
        detail = QVBoxLayout(self.details); detail.setContentsMargins(0, 0, 0, 0); detail.setSpacing(8)
        lyrics = QFrame(objectName='lyrics'); lyrics.setFixedHeight(136); self.lyrics_frame = lyrics
        ly = QVBoxLayout(lyrics); ly.setContentsMargins(12, 8, 12, 8); ly.setSpacing(1)
        self.lyric_head = QWidget(); lhead = QHBoxLayout(self.lyric_head); lhead.setContentsMargins(0,0,0,0); self.lyric_source = QLabel('同步歌词', objectName='section')
        lhead.addWidget(self.lyric_source); lhead.addStretch()
        self.load_lrc = QPushButton('关联 LRC', objectName='text'); self.load_lrc.clicked.connect(self.choose_lrc)
        self.cloud_button = QPushButton('云端匹配', objectName='text'); self.cloud_button.clicked.connect(self.show_cloud)
        lhead.addWidget(self.cloud_button)
        lhead.addWidget(self.load_lrc); ly.addWidget(self.lyric_head)
        self.lyric_before = QLabel('', objectName='lyricSide')
        self.lyric_current = ClampedLabel('等待当前曲目', objectName='lyricCurrent')
        self.lyric_current.mouseDoubleClickEvent = lambda event: self.show_cloud()
        self.lyric_after = QLabel('', objectName='lyricSide')
        for label in (self.lyric_before, self.lyric_current, self.lyric_after):
            label.setWordWrap(True); label.setAlignment(Qt.AlignCenter); label.setTextFormat(Qt.PlainText)
            ly.addWidget(label, 2 if label is self.lyric_current else 1)
        detail.addWidget(lyrics)
        review = QFrame(objectName='review'); self.review_frame = review
        review.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        rv = QVBoxLayout(review); rv.setContentsMargins(12, 8, 12, 8); rv.setSpacing(4)
        self.review_head = QWidget(); rhead = QHBoxLayout(self.review_head); rhead.setContentsMargins(0,0,0,0)
        self.review_source = ElideLabel('LLM 曲评'); self.review_source.setObjectName('section')
        rhead.addWidget(self.review_source, 1)
        self.review_score = QLabel('—', objectName='reviewScore')
        self.review_score.setAccessibleName('正式总分与星级')
        self.review_score.setToolTip('当前录音暂无正式总分')
        self.review_score.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Preferred)
        rhead.addWidget(self.review_score)
        full = QPushButton('全文 ↗', objectName='text'); self.review_full_button = full
        full.clicked.connect(self.show_review)
        rhead.addWidget(full); rv.addWidget(self.review_head)
        self.review = ReviewReader('读取已保存的曲评…', objectName='reviewText')
        self.review.setAccessibleName('可滚动曲评正文')
        rv.addWidget(self.review, 1)
        detail.addWidget(review, 1)
        main.addWidget(self.details, 1)
        self.bottom_resize = ReviewResizeHandle(self, Qt.BottomEdge)
        self.corner_resize = ReviewResizeHandle(self, Qt.BottomEdge | Qt.RightEdge)
        self.set_controls(False)

    def setup_tray(self):
        self.tray = QSystemTrayIcon(self.windowIcon(), self)
        self.tray.setToolTip('JRiver 浮窗')
        menu = QMenu()
        self.input_action = menu.addAction('切换为鼠标穿透', self.toggle_input_mode)
        self.input_status_action = menu.addAction('当前：可操作'); self.input_status_action.setEnabled(False)
        self.hotkey_action = menu.addAction(''); self.hotkey_action.setEnabled(False)
        menu.addSeparator()
        menu.addAction('恢复可操作并显示', self.recover_interactive)
        menu.addAction('显示浮窗', self.reveal)
        menu.addAction('展开 / 收起', self.toggle_expanded)
        self.tray_pin_action = menu.addAction('置顶开关', self.toggle_pin)
        menu.addAction('窗口设置…', self.show_settings)
        menu.addSeparator()
        menu.addAction('退出浮窗', self.close)
        self.tray.setContextMenu(menu)
        self.tray.activated.connect(lambda reason: self.reveal() if reason in (QSystemTrayIcon.Trigger, QSystemTrayIcon.DoubleClick) else None)
        self.tray.show()

    def reveal(self):
        if self.click_through:
            self.setAttribute(Qt.WA_ShowWithoutActivating, True)
            self.showNormal(); self.apply_input_mode()
        else:
            self.setAttribute(Qt.WA_ShowWithoutActivating, False)
            self.showNormal(); self.raise_(); self.activateWindow()

    def recover_interactive(self):
        self.set_click_through(False)
        self.reveal()

    def toggle_input_mode(self):
        # A modal lyric/review editor must not remain over the game while the overlay
        # changes to pass-through. Close only this window's temporary editor/menu.
        modal = QApplication.activeModalWidget()
        if modal and self.isAncestorOf(modal):
            modal.reject()
        popup = QApplication.activePopupWidget()
        if popup:
            popup.close()
        self.set_click_through(not self.click_through)

    def set_click_through(self, enabled):
        previous = self.click_through
        self.click_through = bool(enabled)
        # Both input modes remain visible above ordinary application windows.
        self.pinned = True
        try:
            self.apply_input_mode()
        except OSError as error:
            self.click_through = previous
            self.tray.showMessage('模式切换未完成', str(error), QSystemTrayIcon.Warning, 4000)
            logging.exception('Input mode change failed')
            return
        self.update_pin(); self.update_input_ui(); self.save()

    def apply_input_mode(self):
        self.setAttribute(Qt.WA_ShowWithoutActivating, self.click_through)
        self.refresh_opacity()
        set_input_mode(self.winId(), self.click_through, topmost=self.pinned or self.click_through)

    def refresh_opacity(self):
        opaque_hover = self.hover_clear and not self.click_through and self.underMouse()
        self.setWindowOpacity(1 if opaque_hover else self.opacity / 100)

    def update_input_ui(self):
        if not hasattr(self, 'input_action'):
            return
        name = '鼠标穿透' if self.click_through else '可操作'
        self.input_action.setText('切换为可操作模式' if self.click_through else '切换为鼠标穿透模式')
        self.input_status_action.setText('当前：' + name)
        shortcut = self.mode_hotkey.label if hasattr(self, 'mode_hotkey') else '正在注册快捷键…'
        self.hotkey_action.setText(shortcut)
        self.tray.setToolTip(f'JRiver 浮窗 · {name}\n{shortcut}')
        self.tray.setIcon(icon('music', '#deb980' if self.click_through else '#a9cfbb'))
        self.tray_pin_action.setEnabled(not self.click_through)
        self.brand.setText('JRIVER / 穿透' if self.click_through else 'JRIVER' if self.ui_scale <= .5 or self.mode == 'mini' else
                           'JRIVER / ' + {'full':'完整卡片','compact':'紧凑卡片','mini':'迷你横条'}[self.mode])

    def showEvent(self, event):
        super().showEvent(event)
        if getattr(self, 'input_ready', False):
            if self.click_through:
                self.pinned = True
            self.apply_input_mode()

    def restore_position(self):
        area = QApplication.primaryScreen().availableGeometry()
        x = int(self.config.get('x', area.right() - self.width() - 26))
        y = int(self.config.get('y', area.bottom() - self.height() - 28))
        if not any(s.availableGeometry().contains(QPoint(x + self.width() // 2, y + 24)) for s in QApplication.screens()):
            x, y = area.right() - self.width() - 26, area.bottom() - self.height() - 28
        self.move(x, y)

    def save(self):
        DATA.mkdir(parents=True, exist_ok=True)
        tmp = CONFIG.with_suffix('.tmp')
        tmp.write_text(json.dumps({'x': self.x(), 'y': self.y(), 'pinned': self.pinned,
                                   'expanded': self.expanded, 'mode': self.mode, 'scale': self.ui_scale,
                                   'opacity': self.opacity, 'hover_clear': self.hover_clear,
                                   'click_through': self.click_through, 'custom_sizes': self.custom_sizes}), encoding='utf-8')
        tmp.replace(CONFIG)

    def moveEvent(self, event):
        if hasattr(self, 'save_timer'):
            self.save_timer.start(400)
        super().moveEvent(event)

    def size_slot(self):
        return f'{self.mode}:{int(self.ui_scale*100)}'

    def remember_size(self):
        self.custom_sizes[self.size_slot()] = [self.width(), self.height()]
        self.save()

    def nativeEvent(self, event_type, message):
        if bytes(event_type) == b'windows_generic_MSG':
            msg = wintypes.MSG.from_address(int(message))
            if msg.message == 0x0231:  # WM_ENTERSIZEMOVE
                self.native_resizing = True
            elif msg.message == 0x0232:  # WM_EXITSIZEMOVE
                self.native_resizing = False
                if hasattr(self, 'save_timer'):
                    self.remember_size()
        return super().nativeEvent(event_type, message)

    def resizeEvent(self, event):
        super().resizeEvent(event)
        if hasattr(self, 'bottom_resize'):
            self.bottom_resize.setGeometry(18, self.height()-13, max(1,self.width()-46), 8)
            self.corner_resize.setGeometry(self.width()-22, self.height()-22, 16, 16)
            self.bottom_resize.raise_(); self.corner_resize.raise_()

    def reset_window_size(self):
        self.custom_sizes.pop(self.size_slot(), None)
        self.apply_mode(); self.save()

    def toggle_pin(self):
        if self.click_through:
            return
        self.pinned = not self.pinned
        self.setWindowFlag(Qt.WindowStaysOnTopHint, self.pinned)
        self.update_pin(); self.show(); self.save()

    def update_pin(self):
        self.pin.setIcon(icon('pin', '#b8d7c4' if self.pinned else '#6a7b88'))
        self.pin.setToolTip('取消置顶' if self.pinned else '保持置顶')

    def toggle_expanded(self):
        modes = ('full', 'compact', 'mini')
        self.mode = modes[(modes.index(self.mode) + 1) % 3]
        self.apply_mode()
        screen = self.screen().availableGeometry()
        if self.geometry().bottom() > screen.bottom():
            self.move(self.x(), max(screen.top(), screen.bottom() - self.height()))
        self.save()

    def capture_metrics(self):
        self.metrics = []
        self.layout_metrics = []
        for widget in self.findChildren(QWidget):
            width = widget.width() if widget.minimumWidth() == widget.maximumWidth() else None
            height = widget.height() if widget.minimumHeight() == widget.maximumHeight() else None
            self.metrics.append((widget, width, height))
        from PySide6.QtWidgets import QLayout
        for layout in self.findChildren(QLayout):
            margins = layout.contentsMargins()
            self.layout_metrics.append((layout, (margins.left(), margins.top(), margins.right(), margins.bottom()), layout.spacing()))

    def apply_mode(self):
        s = self.ui_scale
        names = {'full': '完整卡片', 'compact': '紧凑卡片', 'mini': '迷你横条'}
        sizes = {'full': (530, 490), 'compact': (460, 350), 'mini': (460, 190)}
        self.expanded = self.mode == 'full'
        self.details.show()
        self.brand.setText('JRIVER' if s <= .5 or self.mode == 'mini' else 'JRIVER / ' + names[self.mode])
        for widget, width, height in self.metrics:
            if width is not None: widget.setFixedWidth(max(1, round(width * s)))
            if height is not None: widget.setFixedHeight(max(1, round(height * s)))
            if isinstance(widget, QPushButton):
                from PySide6.QtCore import QSize
                widget.setIconSize(QSize(max(10, round(18 * s)), max(10, round(18 * s))))
        for layout, margins, spacing in self.layout_metrics:
            layout.setContentsMargins(*(round(v * s) for v in margins))
            if spacing >= 0: layout.setSpacing(round(spacing * s))
        def scale_css(match):
            return str(max(1, round(float(match[1]) * s))) + 'px'
        css = re.sub(r'(\d+(?:\.\d+)?)px', scale_css, self.base_style)
        # Keep small-mode text readable at Windows' native DPI, while preserving every section.
        css = re.sub(r'font-size: ([\d.]+)px', lambda m: 'font-size: ' + str(max(8, float(m[1]))) + 'px', css)
        self.setStyleSheet(css)
        if self.mode == 'mini':
            self.main_layout.removeWidget(self.control_host)
            self.summary_layout.addWidget(self.control_host)
            self.controls_layout.setDirection(QBoxLayout.TopToBottom)
            self.control_host.setFixedWidth(round(136*s))
            self.header.setFixedHeight(round(22*s))
            self.main_layout.setSpacing(round(5*s))
            self.main_layout.setContentsMargins(round(10*s),round(7*s),round(10*s),round(10*s))
            self.setStyleSheet(css + f'QLabel#title {{font-size:{max(9,14*s)}px;}} QLabel#lyricCurrent {{font-size:{max(8,11*s)}px;}}')
        else:
            self.summary_layout.removeWidget(self.control_host)
            self.main_layout.insertWidget(2, self.control_host)
            self.controls_layout.setDirection(QBoxLayout.LeftToRight)
            self.control_host.setMinimumWidth(0); self.control_host.setMaximumWidth(16777215)
        cover = 82 if self.mode == 'full' else 70 if self.mode == 'compact' else 54
        self.cover.setFixedSize(round(cover*s), round(cover*s))
        self.cover.setStyleSheet(f'background:#2b3942;border-radius:{10*s}px;color:#9fb8ab;font-size:{30*s}px;')
        self.state.setVisible(self.mode != 'mini' and s > .5)
        self.lyric_before.setVisible(self.mode == 'full' and s >= .75)
        self.lyric_after.setVisible(self.mode == 'full' and s >= .75)
        self.lyric_head.setVisible(self.mode != 'mini'); self.review_head.setVisible(True)
        self.lyric_source.setText('歌词' if self.mode == 'mini' else '同步歌词')
        self.load_lrc.setVisible(self.mode == 'full' and s >= .75)
        self.lyrics_frame.setFixedHeight(round((145 if self.mode == 'full' else 88 if self.mode == 'compact' else 30) * s))
        self.review_budget = round((100 if self.mode == 'full' else 72 if self.mode == 'compact' else 50) * s)
        self.review_frame.setVisible(self.reviews_enabled)
        self.review_frame.setMinimumHeight(self.review_budget)
        self.review_frame.setMaximumHeight(16777215)
        self.review_min_lines = {'full': 3, 'compact': 2, 'mini': 1}[self.mode]
        self.lyric_current.setWordWrap(self.mode != 'mini')
        if self.mode == 'mini':
            line = max(self.lyric_current.fontMetrics().height(), self.review.fontMetrics().height())
            linebox = max(round(30*s), 2*round(8*s) + line + 2)
            self.lyrics_frame.setFixedHeight(linebox)
        self.lyric_budget = self.lyrics_frame.height()
        if not self.reviews_enabled:
            self.lyrics_frame.setMinimumHeight(self.lyric_budget)
            self.lyrics_frame.setMaximumHeight(16777215)
            self.lyrics_frame.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        w, h = sizes[self.mode]
        # Smaller text floors require a few extra vertical pixels, not removed information.
        self.base_height = round(h*s) + (24 if s == .5 else 0)
        if not self.reviews_enabled:
            self.base_height -= self.review_budget + round(8*s)
        self.base_width = round(w*s)
        self.setMaximumSize(16777215, 16777215)
        self.setMinimumSize(self.base_width, self.base_height)
        self.restore_size_pending = self.custom_sizes.get(self.size_slot(), [self.base_width, self.base_height])
        self.resize(*self.restore_size_pending)
        self.expand.setIcon(icon('expand' if self.mode == 'mini' else 'collapse'))
        self.expand.setToolTip('切换视窗模式：' + names[self.mode])
        if not self.cover_original.isNull(): self.render_cover()
        self.refresh_opacity()
        if self.input_ready:
            self.apply_input_mode(); self.update_input_ui()
        self.title.refresh(); self.artist.refresh(); self.album.refresh(); self.update_review_header()
        self.review.refresh(); self.lyric_current.refresh()
        QTimer.singleShot(0, self.fit_height)
        screen = self.screen().availableGeometry()
        self.move(max(screen.left(), min(self.x(), screen.right()-self.width()+1)),
                  max(screen.top(), min(self.y(), screen.bottom()-self.height()+1)))

    def fit_height(self):
        # Qt reflows translated/source labels after the first event loop. Honor that
        # minimum instead of forcing fixed card heights to overlap one another.
        # Reserve real font-height rows before clamping text. Borrow space from the
        # lyric card at tiny scales, rather than letting the review collapse to zero.
        rv = self.review_frame.layout(); margins = rv.contentsMargins()
        head = self.review_head.minimumSizeHint().height() + rv.spacing() if self.review_head.isVisible() else 0
        required_review = margins.top() + margins.bottom() + head + self.review.fontMetrics().lineSpacing() * self.review_min_lines + 2
        extra = max(0, required_review - self.review_budget)
        if not self.reviews_enabled:
            self.lyrics_frame.setMinimumHeight(self.lyric_budget)
            self.lyrics_frame.setMaximumHeight(16777215)
        elif extra:
            ly = self.lyrics_frame.layout(); lm = ly.contentsMargins()
            lyric_min = lm.top() + lm.bottom() + self.lyric_current.fontMetrics().lineSpacing() + 2
            if self.lyric_head.isVisible(): lyric_min += self.lyric_head.minimumSizeHint().height() + ly.spacing()
            self.lyrics_frame.setFixedHeight(max(lyric_min, self.lyric_budget - extra))
        else:
            self.lyrics_frame.setFixedHeight(self.lyric_budget)
        self.review_frame.setMinimumHeight(max(self.review_budget, required_review))
        for layout, _, _ in reversed(self.layout_metrics):
            layout.invalidate()
            layout.activate()
        self.layout().activate()
        self.review.refresh()
        required = self.layout().minimumSize().height()
        self.setMinimumHeight(max(self.base_height, required))
        if self.restore_size_pending is not None:
            width, height = self.restore_size_pending
            self.restore_size_pending = None
            screen = self.screen().availableGeometry()
            self.resize(max(self.minimumWidth(), min(width, screen.width())),
                        max(self.minimumHeight(), min(height, screen.height())))
            self.move(max(screen.left(), min(self.x(), screen.right()-self.width()+1)),
                      max(screen.top(), min(self.y(), screen.bottom()-self.height()+1)))
        self.layout().activate()

    def set_option(self, field, value):
        setattr(self, field, value)
        self.apply_mode(); self.save()

    def show_settings(self):
        menu = QMenu(self)
        menu.setStyleSheet('QMenu {font-size:12px;background:#1c252d;color:#dbe4ec;} QMenu::item {padding:7px 14px;} QMenu::item:selected {background:#35434b;}')
        action = menu.addAction('切换为可操作模式' if self.click_through else '切换为鼠标穿透模式')
        action.triggered.connect(self.toggle_input_mode)
        menu.addSeparator()
        for label, field, values in [
            ('视窗模式', 'mode', [('完整卡片', 'full'), ('紧凑卡片', 'compact'), ('迷你横条', 'mini')]),
            ('界面缩放', 'ui_scale', [(f'{int(s*100)}%', s) for s in (.5, .75, 1, 1.25)]),
            ('窗口不透明度', 'opacity', [(f'{s}%' + ('（不透明）' if s == 100 else ''), s) for s in (40, 60, 80, 90, 100)])]:
            sub = menu.addMenu(label); group = QActionGroup(sub); group.setExclusive(True)
            for text, value in values:
                act = sub.addAction(text); act.setCheckable(True); act.setChecked(getattr(self, field) == value); group.addAction(act)
                act.triggered.connect(lambda checked=False, f=field, v=value: self.set_option(f, v))
        hover = menu.addAction('鼠标移入时恢复不透明'); hover.setCheckable(True); hover.setChecked(self.hover_clear)
        hover.triggered.connect(lambda checked: self.set_option('hover_clear', checked))
        menu.addSeparator()
        menu.addAction('恢复当前模式的默认宽高', self.reset_window_size)
        undo = menu.addAction('撤销上次评分', self.undo_rating)
        undo.setEnabled(bool(self.last_rating_change) and not self.control_busy)
        menu.addAction('云端歌词匹配…', self.show_cloud)
        menu.addAction('查看曲评全文…', self.show_review)
        control_url = QUrl(str(SETTINGS.get('control_center_url', '')))
        if control_url.scheme() in ('http', 'https') and control_url.host():
            menu.addAction('打开完整中控台', lambda: QDesktopServices.openUrl(control_url))
        menu.exec(self.settings_button.mapToGlobal(self.settings_button.rect().bottomLeft()))

    def contextMenuEvent(self, event):
        self.show_settings()

    def enterEvent(self, event):
        if self.hover_clear and not self.click_through: self.setWindowOpacity(1)
        super().enterEvent(event)

    def leaveEvent(self, event):
        self.setWindowOpacity(self.opacity / 100)
        if self.input_ready and self.click_through:
            set_input_mode(self.winId(), True, topmost=True)
        super().leaveEvent(event)

    def show_review(self):
        dialog = QDialog(self); dialog.setWindowTitle('当前曲评'); dialog.resize(500, 330)
        dialog.setStyleSheet('QDialog {background:#182129;} QTextBrowser {background:#202d36;color:#e4ebef;border:1px solid #485a64;font-size:14px;padding:10px;}')
        layout = QVBoxLayout(dialog)
        browser = QTextBrowser(); browser.setAccessibleName('曲评完整正文')
        browser.setPlainText(self.title.full + '\n' + self.artist.full + '\n' + self.review_source.full + ' · ' + self.review_score.text() + '\n\n' + (self.review_full_text or self.review.full))
        layout.addWidget(browser)
        dialog.exec()

    def set_controls(self, enabled):
        for widget in (self.play, self.previous, self.next, self.seek, *self.stars):
            widget.setEnabled(enabled and not self.control_busy)
        self.load_lrc.setEnabled(enabled and bool(self.track.get('Filename')))
        self.undo_rating_button.setEnabled(bool(self.last_rating_change) and self.connected and not self.control_busy)

    def poll(self):
        if self.poll_busy:
            return
        self.poll_busy = True
        self.submit(self.bridge.status, self.on_status)

    def submit(self, fn, callback):
        self.next_job += 1
        self.callbacks[self.next_job] = callback
        self.pool.start(Work(fn, self.next_job, self.results))

    @Slot(int, object, object)
    def work_done(self, token, value, error):
        callback = self.callbacks.pop(token, None)
        if callback:
            callback(value, error)

    def on_status(self, info, error):
        self.poll_busy = False
        self.connected = error is None
        if error:
            self.set_controls(False)
            self.state.setText('JRiver 未连接')
            self.state.setToolTip(error)
            self.last_tick = time.monotonic()
            return
        self.info = info
        valid = info.get('FileKey') not in (None, '', '-1')
        self.set_controls(valid)
        newkey = info.get('FileKey') if valid else None
        if newkey != self.key:
            self.key = newkey; self.track = {}; self.lines = []; self.lyric_row = None
            self.cover_original = QPixmap()
            self.cover.setPixmap(QPixmap()); self.cover.setText('♫')
            self.review.setText('读取已保存的曲评…' if valid else '播放一首歌曲后显示曲评')
            self.review_full_text = ''; self.update_review_header()
            self.lyric_before.clear(); self.lyric_after.clear()
            self.lyric_current.setText('读取歌词…' if valid else '等待当前曲目')
            self.last_details = 0
            if valid:
                key = self.key
                self.submit(lambda: self.bridge.cover(key), lambda data, err: self.on_cover(key, data, err))
        self.title.setText(info.get('Name') or '暂无正在播放的曲目')
        self.artist.setText(info.get('Artist') or '在 JRiver 中选择音乐')
        rate = int(info.get('SampleRate') or 0) / 1000
        self.album.setText((info.get('Album') or '') + (f' · {rate:g} kHz' if rate else ''))
        rating = int(float(info.get('Rating') or 0))
        if self.rating_verified and str(self.rating_verified['key']) == str(self.key):
            if rating == self.rating_verified['rating'] or time.monotonic() > self.rating_verified['until']:
                self.rating_verified = None
            else:
                rating = self.rating_verified['rating']
        for i, star in enumerate(self.stars):
            star.setText('★' if i < rating else '☆')
        playing = info.get('State') == '2'
        self.play.setIcon(icon('pause' if playing else 'play', '#21362a'))
        if time.monotonic() > self.message_until:
            self.state.setText('正在播放' if playing else '已暂停' if info.get('State') == '1' else '已停止')
            self.state.setToolTip('')
        self.last_position = int(info.get('PositionMS') or 0)
        self.last_tick = time.monotonic()
        self.seek.setMaximum(max(1, int(info.get('DurationMS') or 0)))
        self.total.setText(self.clock(self.seek.maximum()))
        if valid and not self.detail_busy and time.monotonic() - self.last_details > 30:
            self.get_details()
        self.tick()

    def get_details(self):
        if self.detail_busy or not self.key:
            return
        self.detail_busy = True
        key = self.key
        self.submit(lambda: self.bridge.details(key), lambda track, err: self.on_details(key, track, err))

    def on_details(self, key, track, error):
        self.detail_busy = False
        if key != self.key:
            self.get_details()
            return
        self.last_details = time.monotonic()
        if error:
            if not self.track.get('review', {}).get('status') == 'available':
                self.review.setText('曲评暂不可用，稍后自动重试')
                self.review.setToolTip(error)
                self.review_full_text = ''; self.update_review_header()
            self.lyric_current.setText('歌词读取失败')
            return
        self.track = track
        self.set_controls(self.connected)
        lyrics = track['lyrics']; self.lines = lyrics['lines']; self.lyric_row = None
        self.lyric_source.setText('同步歌词 · ' + lyrics['source'])
        if not self.lines:
            self.lyric_current.setText('暂无同步歌词')
            self.lyric_after.setText('可关联此录音的 LRC 文件')
            self.lyric_current.setToolTip(lyrics.get('plain') or '未找到同名 LRC 或内嵌时间轴歌词')
            self.auto_cloud()
        else:
            cached = self.cloud.cached(track)
            if cached and cached.get('selected', {}).get('manual'):
                self.apply_cloud(cached['selected'])
        review = track.get('review', {})
        if review.get('status') == 'available':
            text = (review.get('comment') or review.get('description') or '').strip() or '这首录音暂无曲评正文'
            self.review.setText(text)
            self.review.setToolTip('在这里滚动阅读，或拖动窗口下沿增加阅读空间')
            self.review_full_text = '\n\n'.join(dict.fromkeys(filter(None, [review.get('description'), text])))
        else:
            self.review.setText('这首录音暂无已保存的 LLM 曲评' if review.get('status') == 'missing' else '曲评库暂不可用')
            self.review.setToolTip('')
            self.review_full_text = ''
        self.update_review_header()
        self.tick()

        QTimer.singleShot(0, self.fit_height)

    def update_review_header(self):
        review = self.track.get('review', {})
        if review.get('status') != 'available':
            self.review_source.setText('LLM 曲评')
            self.review_score.setText('—')
            self.review_score.setToolTip('当前录音暂无正式总分')
            return
        model = review.get('model') or '已保存'
        self.review_source.setText('LLM 曲评 · ' + model)
        self.review_source.setToolTip('曲评模型：' + model)
        score = review.get('final_score')
        valid = isinstance(score, (int, float)) and not isinstance(score, bool) and math.isfinite(score) and 0 <= score <= 100
        score_model = review.get('score_model') or model
        self.review_score.setText(f"总分 {score:.2f} · {review.get('final_star', '—')}星" if valid else '总分待定')
        self.review_score.setToolTip(f"正式总分：{score:.2f}/100\n版本：{review.get('final_score_version','')}\n原单曲模型分：{review.get('track_score','—')}/10（{score_model}）" if valid else '当前录音暂无正式总分')

    def apply_cloud(self, result):
        if result and result.get('text'):
            self.lines = parse_lrc(result['text']); self.lyric_row = None
            self.lyric_source.setText('歌词 · ' + result['source'])
            self.lyric_current.setToolTip('云端来源：' + result['source'])
            self.tick()

    def auto_cloud(self):
        if not SETTINGS.get('automatic_lyrics', False):
            return
        cached = self.cloud.cached(self.track)
        if cached and cached.get('selected'):
            self.apply_cloud(cached['selected']); return
        if self.cloud_busy or not self.track or self.key in self.cloud_attempts:
            return
        self.cloud_busy = True
        key, track = self.key, dict(self.track)
        self.cloud_attempts.add(key)
        self.lyric_current.setText('正在匹配云端歌词…')
        self.submit(lambda: self.cloud.automatic(track), lambda data, err: self.on_auto_cloud(key, data, err))

    def on_auto_cloud(self, key, result, error):
        self.cloud_busy = False
        if key != self.key:
            return
        if not error and result.get('text'):
            self.apply_cloud(result)
        else:
            self.lyric_current.setText('暂无确认匹配的同步歌词')
            self.lyric_after.setText('点击“云端匹配”选择录音版本')
            self.lyric_source.setText('云端暂不可用' if error or result.get('errors') else '可手动选择候选')

    def show_cloud(self):
        if not self.track:
            return
        dialog = QDialog(self)
        dialog.setWindowTitle('云端歌词 · ' + self.track.get('Name', ''))
        dialog.resize(650, 550)
        dialog.setStyleSheet('QDialog {background:#182129;color:#dbe4ec;} QWidget {font-size:12px;} QListWidget,QTextBrowser {background:#222e37;color:#e0e7eb;border:1px solid #46545d;} QPushButton {padding:8px;background:#35474d;color:#dfeae4;border-radius:5px;}')
        track, key = dict(self.track), self.key
        layout = QVBoxLayout(dialog)
        heading = QLabel(track.get('Name', '') + ' / ' + track.get('Artist', '')); heading.setWordWrap(True); heading.setTextFormat(Qt.PlainText)
        layout.addWidget(heading)
        status = QLabel('搜索 LRCLIB、网易云…'); layout.addWidget(status)
        rows = QListWidget(); rows.setWordWrap(True); rows.setMinimumHeight(160); layout.addWidget(rows)
        preview = QTextBrowser(); layout.addWidget(preview)
        buttons = QHBoxLayout(); refresh = QPushButton('重新搜索'); apply = QPushButton('关联此版本'); apply.setEnabled(False)
        buttons.addWidget(refresh); buttons.addStretch(); buttons.addWidget(apply); layout.addLayout(buttons)
        state = {'rows': [], 'candidate': None, 'text': '', 'generation': 0, 'open': True}
        def found(result, error):
            if not state['open']: return
            refresh.setEnabled(True); rows.clear()
            if error:
                status.setText('搜索失败：' + error); return
            state['rows'] = result.get('candidates', [])
            for candidate in state['rows']:
                note = '精确匹配' if candidate['safe'] else '请核对版本'
                label = f"{candidate['source']} · {note} · {candidate['duration']:.0f}s\n{candidate['title']} / {'、'.join(candidate['artists'])}\n{candidate['album']}"
                item = QListWidgetItem(label); rows.addItem(item)
            status.setText(f"找到 {len(state['rows'])} 个候选；选择后预览，再关联。" + (' · ' + '；'.join(result.get('errors', [])) if result.get('errors') else ''))
        def search(force=False):
            refresh.setEnabled(False); apply.setEnabled(False); preview.clear()
            state['generation'] += 1
            self.submit(lambda: self.cloud.search(track, force=force), found)
        def selected(index):
            apply.setEnabled(False); state['text'] = ''; state['generation'] += 1
            if index < 0 or index >= len(state['rows']): return
            candidate = state['rows'][index]; generation = state['generation']; state['candidate'] = candidate
            preview.setPlainText('读取歌词…')
            def loaded(text, error):
                if not state['open'] or generation != state['generation']: return
                if error or not parse_lrc(text or ''):
                    preview.setPlainText('此候选暂无同步歌词' if not error else '获取失败：' + error); return
                state['text'] = text
                preview.setPlainText(text); apply.setEnabled(True)
            self.submit(lambda: self.cloud.fetch(candidate), loaded)
        def adopt():
            if not state['text'] or not state['candidate']: return
            result = self.cloud.select(track, state['candidate'], state['text'], manual=True)
            if key == self.key: self.apply_cloud(result)
            dialog.accept()
        rows.currentRowChanged.connect(selected)
        refresh.clicked.connect(lambda: search(True)); apply.clicked.connect(adopt)
        search(); dialog.exec(); state['open'] = False

    def on_cover(self, key, data, error):
        if key != self.key or error:
            return
        pix = QPixmap()
        if pix.loadFromData(data):
            self.cover_original = pix
            self.render_cover()

    def render_cover(self):
            pix = self.cover_original
            scaled = pix.scaled(164, 164, Qt.KeepAspectRatioByExpanding, Qt.SmoothTransformation)
            rounded = QPixmap(164, 164); rounded.fill(Qt.transparent)
            p = QPainter(rounded); p.setRenderHint(QPainter.Antialiasing)
            path = QPainterPath(); path.addRoundedRect(QRectF(0, 0, 164, 164), 20, 20)
            p.setClipPath(path); p.drawPixmap((164 - scaled.width()) // 2, (164 - scaled.height()) // 2, scaled); p.end()
            rounded.setDevicePixelRatio(164 / self.cover.width())
            self.cover.setPixmap(rounded)

    @staticmethod
    def clock(ms):
        seconds = max(0, int(ms) // 1000)
        return f'{seconds // 60}:{seconds % 60:02d}'

    def tick(self):
        position = self.last_position
        if self.connected and self.info.get('State') == '2':
            position += int((time.monotonic() - self.last_tick) * 1000)
        position = min(position, self.seek.maximum())
        if not self.seek.isSliderDown():
            self.seek.setValue(position)
        self.elapsed.setText(self.clock(self.seek.value()))
        if self.lines:
            index = lyric_index(self.lines, position)
            if index != self.lyric_row:
                self.lyric_row = index
                self.lyric_before.setText(self.lines[index - 1][1] if index > 0 else '')
                self.lyric_current.setText((self.lines[index][1] or '♪') if index >= 0 else '♪')
                self.lyric_after.setText(self.lines[index + 1][1] if index + 1 < len(self.lines) else '')
                self.lyric_current.setToolTip(self.lines[index][1] if index >= 0 else '')

    def seek_to(self):
        if self.seek_context != (self.key, self.info.get('ZoneID', '-1')):
            self.tick()
            return
        self.command('seek', value=self.seek.value())

    def play_pause(self):
        self.command('Play' if self.info.get('State') == '0' else 'Pause')

    def command(self, action, value=None):
        if not self.connected or not self.key or self.control_busy:
            return
        key, zone = self.key, self.info.get('ZoneID', '-1')
        self.control_busy = True; self.set_controls(False)
        if action == 'rating':
            self.state.setText('保存星级…'); self.message_until = time.monotonic() + 10
            self.submit(lambda: self.bridge.command(action, key=key, value=value, zone=zone), self.on_rating)
        else:
            self.submit(lambda: self.bridge.command(action, key=key, value=value, zone=zone), self.on_command)

    def on_rating(self, result, error, undo=False):
        self.control_busy = False
        if error:
            self.state.setText('评分未保存'); self.state.setToolTip(error)
            for star in self.stars: star.setToolTip(error)
            logging.warning('Rating write failed: %s', error)
        elif result:
            if undo:
                self.last_rating_change = None
            elif result['before'] != result['rating']:
                self.last_rating_change = dict(result)
            self.rating_verified = {**result, 'until': time.monotonic() + 2}
            if str(result['key']) == str(self.key):
                self.info['Rating'] = str(result['rating'])
                for i, star in enumerate(self.stars): star.setText('★' if i < result['rating'] else '☆')
            self.state.setText(('已撤销 · ' if undo else '已保存 · ') + str(result['rating']) + '★')
            self.state.setToolTip(result.get('name', ''))
            for i, star in enumerate(self.stars): star.setToolTip(f'将当前曲目设为 {i+1} 星；右键可取消评分')
        self.message_until = time.monotonic() + 6
        if self.last_rating_change:
            change = self.last_rating_change
            self.undo_rating_button.setToolTip(f"撤销上次评分：{change.get('name','')}\n{change['rating']} → {change['before']} 星")
        else:
            self.undo_rating_button.setToolTip('暂无可撤销的评分')
        self.set_controls(self.connected); self.poll()

    def undo_rating(self):
        if not self.last_rating_change or self.control_busy or not self.connected:
            return
        change = dict(self.last_rating_change)
        self.control_busy = True; self.set_controls(False)
        self.submit(lambda: self.bridge.set_rating(change['key'], change['before'], expected=change['rating']),
                    lambda result, error: self.on_rating(result, error, undo=True))

    def show_rating_menu(self):
        menu = QMenu(self)
        clear = menu.addAction('取消当前曲目评分（0 星）', lambda: self.command('rating', value=0))
        clear.setEnabled(self.connected and not self.control_busy)
        undo = menu.addAction('撤销上次评分', self.undo_rating)
        undo.setEnabled(bool(self.last_rating_change) and not self.control_busy)
        menu.exec(self.stars[0].mapToGlobal(self.stars[0].rect().bottomLeft()))

    def on_command(self, result, error):
        self.control_busy = False; self.set_controls(self.connected)
        if error:
            self.state.setText('操作未完成'); self.state.setToolTip(error)
            self.message_until = time.monotonic() + 6
        self.poll()

    def choose_lrc(self):
        filename = self.track.get('Filename')
        if not filename:
            return
        path, _ = QFileDialog.getOpenFileName(self, '关联当前录音的同步歌词', '', 'LRC 歌词 (*.lrc);;所有文件 (*)')
        if not path:
            return
        try:
            import_lyrics(filename, path)
            cached = self.cloud.cached(self.track)
            if cached and 'selected' in cached:
                cached.pop('selected'); self.cloud.store(self.track, cached)
            self.last_details = 0
            self.get_details()
        except (OSError, ValueError) as error:
            self.state.setText('歌词关联失败'); self.state.setToolTip(str(error))
            self.message_until = time.monotonic() + 6

    def closeEvent(self, event):
        self.closing = True
        if hasattr(self, 'mode_hotkey'):
            self.mode_hotkey.close()
        self.save()
        self.poll_timer.stop(); self.tick_timer.stop(); self.tray.hide()
        event.accept()
        QApplication.instance().quit()


def main():
    DATA.mkdir(parents=True, exist_ok=True)
    handler = RotatingFileHandler(DATA / 'floating.log', maxBytes=512000, backupCount=2, encoding='utf-8')
    logging.basicConfig(level=logging.INFO, handlers=[handler], format='%(asctime)s %(message)s')
    sys.excepthook = lambda typ, value, tb: logging.error('Unhandled exception', exc_info=(typ, value, tb))
    app = QApplication(sys.argv)
    app.setApplicationName('JRiver 浮窗')
    app.setQuitOnLastWindowClosed(False)
    name = 'JRiverFloatingCard.v1'
    existing = QLocalSocket()
    existing.connectToServer(name)
    if existing.waitForConnected(400):
        existing.write(b'show'); existing.waitForBytesWritten(400); existing.disconnectFromServer()
        return 0
    QLocalServer.removeServer(name)
    server = QLocalServer()
    if not server.listen(name):
        raise RuntimeError('浮窗已在启动中，请稍后重试')
    player = Player()
    def activate():
        connection = server.nextPendingConnection()
        if connection:
            connection.close(); connection.deleteLater()
        player.reveal()
    server.newConnection.connect(activate)
    player.show()
    result = app.exec()
    player.pool.waitForDone(5000)
    server.close()
    return result


if __name__ == '__main__':
    raise SystemExit(main())

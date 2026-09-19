"""Visible native QA with a simulated player and isolated preferences; no JRiver writes."""
import ctypes as c
from ctypes import wintypes as w
import importlib.util
from importlib.machinery import SourceFileLoader
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import time

from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication, QPushButton
from PySide6.QtTest import QTest

ROOT=Path(__file__).resolve().parent
OUT=ROOT/'.qa';OUT.mkdir(exist_ok=True)

if '--target' in sys.argv:
    countfile=Path(sys.argv[-1]);app=QApplication([])
    button=QPushButton('Controlled click target');button.setWindowTitle('JRiver Card QA target')
    button.setGeometry(80,80,850,650);counter=[0]
    def hit():
        counter[0]+=1;countfile.write_text(str(counter[0]))
    button.clicked.connect(hit);button.show();app.exec();raise SystemExit()

temp=tempfile.TemporaryDirectory();os.environ['JRIVER_CARD_DATA']=temp.name
os.environ['JRIVER_CARD_CONFIG']=str(Path(temp.name)/'config.json')
loader=SourceFileLoader('card',str(ROOT/'floating_player.pyw'))
spec=importlib.util.spec_from_loader(loader.name,loader);m=importlib.util.module_from_spec(spec);loader.exec_module(m)
app=QApplication([]);app.setQuitOnLastWindowClosed(False)
class Fixture:
    def __init__(self):self.calls=[]
    def status(self):return dict(FileKey='1',Name='Starlit Walk',Artist='Example Artist',Album='Night Sessions',
                                ZoneID='0',DurationMS='240000',PositionMS='12000',State='1',Rating='4',Status='Paused')
    def details(self,key):return dict(Filename='C:/Example.flac',Name='Starlit Walk',Artist='Example Artist',Album='Night Sessions',
         Duration=240,review={'status':'missing'},lyrics={'lines':[(0,'A small window for your music'),(10000,'音乐与歌词，随手可见'),(20000,'Keep listening')],'plain':'','source':'示例 LRC'})
    def cover(self,key):return b''
    def command(self,action,**kwargs):self.calls.append(action);return {}

bridge=Fixture();p=None;target=None;result={}
u=c.WinDLL('user32');u.GetWindowRect.argtypes=[w.HWND,c.POINTER(w.RECT)]
u.GetWindowLongW.argtypes=[w.HWND,c.c_int]
saved_cursor=w.POINT();u.GetCursorPos(c.byref(saved_cursor))
def wait(predicate,seconds=5):
    deadline=time.monotonic()+seconds
    while time.monotonic()<deadline:
        QTest.qWait(40)
        if predicate():return
    raise AssertionError('QA timed out')
def clicks(file):return int(file.read_text()) if file.exists() else 0
def click_card():
    rect=w.RECT();assert u.GetWindowRect(int(p.winId()),c.byref(rect))
    u.SetCursorPos(rect.left+45,rect.top+65);u.mouse_event(2,0,0,0,0);QTest.qWait(60);u.mouse_event(4,0,0,0,0);QTest.qWait(220)
try:
    countfile=Path(temp.name)/'clicks.txt'
    target=subprocess.Popen([sys.executable,str(Path(__file__).resolve()),'--target',str(countfile)],creationflags=subprocess.CREATE_NO_WINDOW)
    QTest.qWait(700)
    p=m.Player(bridge=bridge);p.setWindowTitle('JRiver Floating Card QA');p.move(100,100);p.show()
    wait(lambda:p.connected and bool(p.track))
    assert not p.review_frame.isVisible()
    layouts=[]
    for mode in ('full','compact','mini'):
        for scale in (.5,.75,1,1.25):
            p.mode=mode;p.ui_scale=scale;p.apply_mode();QTest.qWait(80)
            assert p.play.isVisible() and p.lyric_current.isVisible()
            assert p.width()>=p.minimumWidth() and p.height()>=p.minimumHeight()
            assert not p.review_frame.isVisible()
            layouts.append(dict(mode=mode,scale=scale,width=p.width(),height=p.height()))
            if scale in (.5,1):p.grab().save(str(OUT/('card-'+mode+'-'+str(scale)+'.png')))
    p.mode='compact';p.ui_scale=1;p.apply_mode();p.move(100,100);QTest.qWait(150)
    p.grab().save(str(OUT/'card-compact.png'))
    old_height=p.lyrics_frame.height();p.resize(p.width()+40,p.height()+100);QTest.qWait(100)
    assert p.lyrics_frame.height()>old_height
    QTest.mouseClick(p.next,Qt.LeftButton);wait(lambda:'Next' in bridge.calls)
    click_card();assert clicks(countfile)==0
    p.input_action.trigger();QTest.qWait(120)
    assert p.click_through and u.GetWindowLongW(int(p.winId()),-20)&0x20
    click_card();assert clicks(countfile)==1
    assert p.mode_hotkey.registered, p.mode_hotkey.label
    keys=[0x11,0x12]+([0x10] if p.mode_hotkey.modifiers&4 else [])+[0x79]
    try:
        for key in keys:u.keybd_event(key,0,0,0)
        QTest.qWait(80)
    finally:
        for key in reversed(keys):u.keybd_event(key,0,2,0)
    wait(lambda:not p.click_through)
    assert not u.GetWindowLongW(int(p.winId()),-20)&0x20
    click_card();assert clicks(countfile)==1
    hide=next(b for b in p.findChildren(QPushButton) if b.accessibleName()=='收起到托盘')
    QTest.mouseClick(hide,Qt.LeftButton);assert not p.isVisible()
    next(a for a in p.tray.contextMenu().actions() if a.text()=='显示浮窗').trigger();QTest.qWait(80);assert p.isVisible()
    result=dict(layouts=layouts,review_optional=True,resize_grows_lyrics=True,play_control=True,
                real_click_through=True,global_hotkey=p.mode_hotkey.label,tray_restore=True)
    p.close();p.pool.waitForDone(5000);assert not p.isVisible() and not p.mode_hotkey.registered
    result['close_releases_hotkey']=True
    (OUT/'window-acceptance.json').write_text(json.dumps(result,ensure_ascii=False,indent=2),'utf-8')
    print(json.dumps(result,ensure_ascii=False))
finally:
    if p is not None and not p.closing:p.close();p.pool.waitForDone(5000)
    if target is not None:target.terminate();target.wait(timeout=5)
    u.SetCursorPos(saved_cursor.x,saved_cursor.y)
    temp.cleanup()

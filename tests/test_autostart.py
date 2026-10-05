import plistlib

from photo_tidy import autostart


def test_plist_starts_launcher_app_via_open_at_login(tmp_path):
    app = tmp_path / "photo-tidy server.app"
    d = plistlib.loads(autostart.plist_xml(app).encode())
    assert d["Label"] == autostart.LABEL and d["RunAtLoad"] is True
    # LaunchServices(open)로 띄워야 이 앱이 '책임 프로세스'가 되어 전체 디스크 접근 권한이 적용됨
    assert d["ProgramArguments"][:4] == ["/usr/bin/open", "-g", "-a", str(app)]
    assert d["ProgramArguments"][4:] == ["--args", "--tailscale", "--no-open"]


def test_install_and_uninstall(tmp_path):
    app, plist, calls, built = tmp_path / "S.app", tmp_path / "agents/x.plist", [], []
    run = lambda cmd, **kw: calls.append(cmd)  # noqa: E731
    autostart.install(app=app, plist=plist, build=built.append, run=run)
    assert built == [app] and plist.exists()
    assert ["launchctl", "bootstrap"] == calls[-1][:2] and calls[-1][-1] == str(plist)
    assert any(c[0] == "pkill" for c in calls)  # 다시 설치 = 떠 있는 서버를 새 코드로 재시작
    calls.clear()
    autostart.uninstall(app=app, plist=plist, run=run)
    assert not plist.exists()
    assert calls[0][:2] == ["launchctl", "bootout"] and calls[1][0] == "pkill"  # 떠 있는 런처(→ 서버)도 종료

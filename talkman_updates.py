"""Desktop 独立更新源；校验安装包，在保存退出后替换，失败保留旧版。"""
from __future__ import annotations

import hashlib
import json
import os
import platform
import plistlib
import re
import shutil
import ssl
import subprocess
import sys
import tempfile
import zipfile
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from urllib import request
from urllib.parse import urlsplit

import certifi

REPOSITORY = 'stevewudongyue-rgb/TalkMan-Desktop'
RELEASES_URL = f'https://api.github.com/repos/{REPOSITORY}/releases?per_page=30'
DOWNLOAD_PREFIX = f'https://github.com/{REPOSITORY}/releases/download/'
RELEASES_PAGE = f'https://github.com/{REPOSITORY}/releases'
APP_NAME = 'TalkMan.app'
BUNDLE_ID = 'com.cololight.talkman'
MAX_PACKAGE = 400_000_000
MAX_EXPANDED = 1_500_000_000
WINDOWS_FILES = ('TalkMan.exe', '_internal', 'TalkMan-软件使用协议.md',
                 'TalkMan-隐私说明.md', 'TalkMan-第三方组件说明.md', 'TalkMan-开始使用.txt')


class UpdateError(Exception):
    pass


@dataclass(frozen=True)
class UpdateInfo:
    version: str
    platform: str
    url: str
    sha256: str
    size: int


def version_tuple(value):
    if not isinstance(value, str) or not re.fullmatch(r'\d{1,5}\.\d{1,5}\.\d{1,5}', value):
        raise UpdateError('更新版本号格式不正确。')
    return tuple(map(int, value.split('.')))


def platform_key():
    if sys.platform == 'darwin' and platform.machine() == 'arm64':
        return 'mac-arm64'
    if sys.platform == 'win32' and platform.machine().lower() in ('amd64', 'x86_64'):
        return 'windows-x64'
    raise UpdateError('当前系统尚不支持应用内升级。')


def _valid_https(url):
    p = urlsplit(url)
    return (p.scheme == 'https' and not p.username and not p.password and p.port in (None, 443)
            and p.hostname in ('api.github.com', 'github.com', 'release-assets.githubusercontent.com',
                               'objects.githubusercontent.com'))


class _ReleaseRedirect(request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        if not _valid_https(newurl):
            raise UpdateError('更新下载跳转到未知地址，已停止下载。')
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _opener():
    return request.build_opener(request.HTTPSHandler(context=ssl.create_default_context(cafile=certifi.where())),
                                _ReleaseRedirect())


def _request(url):
    return request.Request(url, headers={'User-Agent': 'TalkMan-Desktop-Updater', 'Accept': 'application/vnd.github+json'})


def parse_releases(payload, current, system):
    """公开仓库目前发布测试版，因此读取 release 列表，严格匹配 Desktop 附件。"""
    if system not in ('windows-x64', 'mac-arm64'):
        raise UpdateError('当前系统尚不支持应用内升级。')
    if len(payload) > 2_000_000:
        raise UpdateError('更新列表过大。')
    try:
        releases = json.loads(payload)
        if not isinstance(releases, list):
            raise ValueError
        candidates = []
        for release in releases:
            if not isinstance(release, dict) or release.get('draft') is not False:
                continue
            tag = release.get('tag_name', '')
            match = re.fullmatch(r'v(\d{1,5}\.\d{1,5}\.\d{1,5})(-test)?', tag)
            if not match or version_tuple(match[1]) <= version_tuple(current):
                continue
            version = match[1]
            name = (f'TalkMan-Windows-Portable-{version}.zip' if system == 'windows-x64'
                    else f'TalkMan-Mac-AppleSilicon-{version}{match[2] or ""}.dmg')
            assets = [a for a in release.get('assets', []) if a.get('name') == name]
            if not assets:
                continue  # 允许分阶段上传；没有本系统安装包时不会提示升级。
            if len(assets) != 1:
                raise ValueError
            asset = assets[0]
            url, digest, size = asset['browser_download_url'], asset['digest'], asset['size']
            if (url != DOWNLOAD_PREFIX + tag + '/' + name or not isinstance(digest, str)
                    or not re.fullmatch(r'sha256:[0-9a-f]{64}', digest)
                    or type(size) is not int or not 0 < size <= MAX_PACKAGE):
                raise ValueError
            candidates.append(UpdateInfo(version, system, url, digest[7:], size))
        return max(candidates, key=lambda item: version_tuple(item.version), default=None)
    except (KeyError, TypeError, ValueError, AttributeError):
        raise UpdateError('更新列表校验失败，当前版本未改变。') from None


def check_update(current, *, opener=None, system=None):
    try:
        with (opener or _opener()).open(_request(RELEASES_URL), timeout=15) as response:
            return parse_releases(response.read(2_000_001), current, system or platform_key())
    except UpdateError:
        raise
    except (OSError, ValueError):
        raise UpdateError('暂时无法连接 GitHub，请稍后重试或打开下载页。') from None


def download_update(info, *, opener=None, progress=None, cancelled=None):
    if not info.url.startswith(DOWNLOAD_PREFIX) or not _valid_https(info.url):
        raise UpdateError('安装包地址不正确。')
    folder = Path(tempfile.mkdtemp(prefix='talkman-desktop-download-'))
    package = folder / info.url.rsplit('/', 1)[-1]
    count, digest = 0, hashlib.sha256()
    try:
        with (opener or _opener()).open(_request(info.url), timeout=40) as response, package.open('xb') as output:
            while chunk := response.read(256 * 1024):
                if cancelled and cancelled.is_set():
                    raise UpdateError('下载已取消，当前版本未改变。')
                count += len(chunk)
                if count > info.size:
                    raise UpdateError('安装包大小不符，已停止升级。')
                output.write(chunk)
                digest.update(chunk)
                if progress:
                    progress(count, info.size)
        if count != info.size or digest.hexdigest() != info.sha256:
            raise UpdateError('安装包校验失败，当前版本未改变。')
        return package
    except Exception as exc:
        shutil.rmtree(folder, ignore_errors=True)
        if isinstance(exc, UpdateError):
            raise
        raise UpdateError('安装包下载失败，请重试；当前版本未改变。') from None


def installed_app_path():
    if not getattr(sys, 'frozen', False):
        raise UpdateError('源码运行时仅检查版本；请用打包后的程序进行升级。')
    exe = Path(sys.executable).resolve()
    if sys.platform == 'darwin':
        app = next((p for p in exe.parents if p.name == APP_NAME), None)
        if app is None or str(app).startswith('/Volumes/') or '/AppTranslocation/' in str(app):
            raise UpdateError('请先把 TalkMan.app 拖到“应用程序”，从那里打开后再升级。')
        return app
    if sys.platform == 'win32' and exe.name.lower() == 'talkman.exe' and (exe.parent / '_internal').is_dir():
        return exe.parent
    raise UpdateError('未找到完整的 TalkMan 程序目录，请先完整解压安装包。')


def prepare_windows_update(package, installed):
    """解压到同盘临时目录，仅接受自己的完整便携包，不触碰用户数据。"""
    stage_root = None
    try:
        stage_root = Path(tempfile.mkdtemp(prefix='.talkman-desktop-update-', dir=installed.parent))
        stage = stage_root / 'TalkMan'
        with zipfile.ZipFile(package) as archive:
            entries = archive.infolist()
            seen, total = set(), 0
            if not entries or len(entries) > 12000:
                raise ValueError
            for entry in entries:
                path = PurePosixPath(entry.filename)
                parts = path.parts
                name = path.as_posix().casefold()
                total += entry.file_size
                if (not parts or parts[0] != 'TalkMan' or len(parts) < 2 or parts[1] not in WINDOWS_FILES
                        or path.is_absolute() or '..' in parts or '.' in entry.filename.split('/')
                        or '\\' in entry.orig_filename or ':' in entry.filename or '//' in entry.filename
                        or any(p.endswith((' ', '.')) for p in parts) or name in seen
                        or (entry.external_attr >> 16) & 0o170000 == 0o120000
                        or total > MAX_EXPANDED or (len(parts) > 2 and parts[1] != '_internal')):
                    raise ValueError
                seen.add(name)
            archive.extractall(stage_root)
        if ((stage / 'TalkMan.exe').read_bytes()[:2] != b'MZ'
                or not (stage / '_internal').is_dir()
                or any(not (stage / name).exists() for name in WINDOWS_FILES)):
            raise ValueError
        return stage
    except (OSError, ValueError, zipfile.BadZipFile, RuntimeError):
        if stage_root:
            shutil.rmtree(stage_root, ignore_errors=True)
        raise UpdateError('安装包结构不正确或目录不可写，当前版本未改变。') from None


def _verify_mac_app(app, version):
    try:
        with (app / 'Contents/Info.plist').open('rb') as stream:
            info = plistlib.load(stream)
        if info.get('CFBundleIdentifier') != BUNDLE_ID or info.get('CFBundleShortVersionString') != version:
            raise ValueError
        subprocess.run(['/usr/bin/codesign', '--verify', '--deep', '--strict', str(app)],
                       check=True, capture_output=True, timeout=60)
    except (OSError, ValueError, subprocess.SubprocessError):
        raise UpdateError('Mac 应用标识、版本或签名校验失败，当前版本未改变。') from None


def verify_mac_signing_continuity(installed, staged):
    """正式签名安装不能被不同团队或临时签名覆盖，尽量沿用系统权限身份。"""
    try:
        def details(app):
            result = subprocess.run(['/usr/bin/codesign', '-d', '-r-', '--verbose=2', str(app)],
                                    check=True, capture_output=True, text=True, timeout=30)
            return result.stdout+'\n'+result.stderr
        before = details(installed)
        team = re.search(r'^TeamIdentifier=([A-Z0-9]{10})$', before, re.MULTILINE)
        if not team:
            return False  # 旧测试签名无法承诺跨版本保留 TCC 授权。
        after = details(staged)
        if not re.search(r'^TeamIdentifier='+re.escape(team[1])+r'$', after, re.MULTILINE):
            raise UpdateError('新版签名身份与当前正式安装不一致，已保留旧版；请联系维护者提供同一开发者签名的安装包。')
        requirement = re.search(r'^designated => (.+)$', before, re.MULTILINE)
        if not requirement:
            raise UpdateError('无法核对当前 Mac 签名身份，已保留旧版。')
        subprocess.run(['/usr/bin/codesign', '--verify', '--strict', '-R', requirement[1], str(staged)],
                       check=True, capture_output=True, timeout=60)
        return True
    except (OSError, subprocess.SubprocessError):
        raise UpdateError('无法核对 Mac 升级签名连续性，已保留旧版。') from None


def prepare_mac_update(package, version, installed):
    stage_root = None
    mount, attached, ready = package.parent / 'mounted', False, False
    try:
        stage_root = Path(tempfile.mkdtemp(prefix='.talkman-desktop-update-', dir=installed.parent))
        stage = stage_root / APP_NAME
        mount.mkdir()
        subprocess.run(['/usr/bin/hdiutil', 'attach', '-readonly', '-nobrowse', '-mountpoint', str(mount), str(package)],
                       check=True, capture_output=True, timeout=90)
        attached = True
        source = mount / APP_NAME
        if not source.is_dir() or not source.resolve().is_relative_to(mount.resolve()):
            raise UpdateError('安装镜像中未找到 TalkMan.app。')
        _verify_mac_app(source, version)
        subprocess.run(['/usr/bin/ditto', str(source), str(stage)], check=True, capture_output=True, timeout=120)
        _verify_mac_app(stage, version)
        verify_mac_signing_continuity(installed, stage)
        ready = True
        return stage
    except UpdateError:
        raise
    except (OSError, subprocess.SubprocessError):
        raise UpdateError('无法准备 Mac 更新，请确认应用目录可写；当前版本未改变。') from None
    finally:
        if attached:
            try:
                subprocess.run(['/usr/bin/hdiutil', 'detach', str(mount), '-quiet'], capture_output=True, timeout=45)
            except (OSError, subprocess.SubprocessError):
                pass
        if not ready and stage_root:
            shutil.rmtree(stage_root, ignore_errors=True)


# 参数通过文件传递，避免用户名中的空格、单引号或中文变成 shell 代码。
WINDOWS_HELPER = r'''param([string]$Config)
$ErrorActionPreference = 'Stop'
$c = Get-Content -LiteralPath $Config -Raw -Encoding UTF8 | ConvertFrom-Json
$target = [IO.Path]::GetFullPath($c.target)
$stage = [IO.Path]::GetFullPath($c.stage)
$root = [IO.Path]::GetDirectoryName($stage)
if (-not ([IO.Path]::GetFileName($root)).StartsWith('.talkman-desktop-update-')) { exit 2 }
if ([IO.Path]::GetDirectoryName($root) -ne [IO.Path]::GetDirectoryName($target)) { exit 2 }
$backup = Join-Path $root 'previous'
$moved = @(); $placed = @()
function Result($text) { [IO.File]::WriteAllText($c.result, $text, [Text.Encoding]::UTF8) }
try {
    $deadline = (Get-Date).AddSeconds(90)
    while (-not (Test-Path -LiteralPath $c.ready)) {
        if ((Get-Date) -gt $deadline) { throw '应用尚未完成保存退出，未安装更新。' }
        Start-Sleep -Milliseconds 250
    }
    if (Get-Process -Id $c.pid -ErrorAction SilentlyContinue) {
        Wait-Process -Id $c.pid -Timeout 60 -ErrorAction SilentlyContinue
    }
    if (Get-Process -Id $c.pid -ErrorAction SilentlyContinue) { throw '旧程序尚未退出，未安装更新。' }
    New-Item -ItemType Directory -Path $backup -ErrorAction Stop | Out-Null
    foreach ($name in $c.files) {
        $source = Join-Path $stage $name
        if (-not (Test-Path -LiteralPath $source)) { throw '新安装包文件缺失。' }
        $old = Join-Path $target $name
        if (Test-Path -LiteralPath $old) {
            Move-Item -LiteralPath $old -Destination (Join-Path $backup $name)
            $moved += $name
        }
        Move-Item -LiteralPath $source -Destination $old
        $placed += $name
    }
    if ($c.restart) {
        $env:TALKMAN_UPDATE_RECEIPT = $c.receipt
        $process = Start-Process -FilePath (Join-Path $target 'TalkMan.exe') -WorkingDirectory $target -PassThru
        $deadline = (Get-Date).AddSeconds(60)
        while (-not (Test-Path -LiteralPath $c.receipt)) {
            if ($process.HasExited) { throw '新版未能启动，正在恢复旧版。' }
            if ((Get-Date) -gt $deadline) {
                Result '更新已安装，但启动尚未确认。旧版备份保存在程序旁的更新目录。'
                exit 1
            }
            Start-Sleep -Milliseconds 250
        }
    }
    Result '更新成功。旧版备份保存在程序旁的更新目录。'
} catch {
    $reason = $_.Exception.Message
    try {
        foreach ($name in $placed) {
            Move-Item -LiteralPath (Join-Path $target $name) -Destination (Join-Path $stage $name)
        }
        foreach ($name in $moved) {
            Move-Item -LiteralPath (Join-Path $backup $name) -Destination (Join-Path $target $name)
        }
        Result ('更新未完成，已保留或恢复原版。' + $reason)
        if ($c.restart -and $moved.Count -gt 0) {
            Remove-Item Env:TALKMAN_UPDATE_RECEIPT -ErrorAction SilentlyContinue
            Start-Process -FilePath (Join-Path $target 'TalkMan.exe') -WorkingDirectory $target
        }
    } catch { Result ('更新未完成，请保留程序旁的更新目录以恢复备份。' + $reason) }
    exit 1
}
'''

MAC_HELPER = '''#!/bin/sh
pid="$1"; stage="$2"; target="$3"; ready="$4"; result="$5"; receipt="$6"
count=0
while [ ! -f "$ready" ]; do
    if [ "$count" -ge 90 ]; then printf '%s' '应用尚未完成保存退出，未安装更新。' > "$result"; exit 1; fi
    sleep 1; count=$((count + 1))
done
count=0
while kill -0 "$pid" 2>/dev/null; do
    if [ "$count" -ge 60 ]; then printf '%s' '旧程序尚未退出，未安装更新。' > "$result"; exit 1; fi
    sleep 1; count=$((count + 1))
done
backup="$(dirname "$stage")/previous"
mkdir "$backup" || exit 1
mv "$target" "$backup/" || exit 1
if ! mv "$stage" "$target"; then
    mv "$backup/TalkMan.app" "$target"
    printf '%s' '更新未完成，已恢复原版。' > "$result"
    /usr/bin/open "$target"
    exit 1
fi
if ! /usr/bin/open --env "TALKMAN_UPDATE_RECEIPT=$receipt" "$target"; then
    mv "$target" "$stage"
    mv "$backup/TalkMan.app" "$target"
    printf '%s' '新版未能打开，已恢复原版。' > "$result"
    /usr/bin/open "$target"
    exit 1
fi
count=0
while [ ! -f "$receipt" ]; do
    if [ "$count" -ge 60 ]; then printf '%s' '更新已安装，但启动尚未确认。旧版备份保存在程序旁的更新目录。' > "$result"; exit 1; fi
    sleep 1; count=$((count + 1))
done
printf '%s' '更新成功。旧版备份保存在程序旁的更新目录。' > "$result"
'''


def launch_installer(package, stage, installed, result):
    folder = package.parent
    ready, receipt = folder / 'ready', folder / 'started'
    if sys.platform == 'win32':
        helper, config = folder / 'install.ps1', folder / 'install.json'
        helper.write_text(WINDOWS_HELPER, encoding='utf-8-sig')
        config.write_text(json.dumps(dict(pid=os.getpid(), stage=str(stage), target=str(installed), ready=str(ready),
                                         result=str(result), receipt=str(receipt), restart=True, files=WINDOWS_FILES)), encoding='utf-8')
        shell = Path(os.environ['SystemRoot']) / 'System32/WindowsPowerShell/v1.0/powershell.exe'
        command = [str(shell), '-NoProfile', '-NonInteractive', '-ExecutionPolicy', 'Bypass', '-File', str(helper), '-Config', str(config)]
    elif sys.platform == 'darwin':
        helper = folder / 'install.sh'
        helper.write_text(MAC_HELPER, encoding='utf-8')
        command = ['/bin/sh', str(helper), str(os.getpid()), str(stage), str(installed), str(ready), str(result), str(receipt)]
    else:
        raise UpdateError('当前系统尚不支持应用内升级。')
    try:
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
                                   start_new_session=True, creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0))
    except OSError:
        raise UpdateError('升级程序未能启动，当前版本未改变。') from None
    return ready, process


def discard_prepared_update(package, stage=None):
    if stage and stage.parent.name.startswith('.talkman-desktop-update-'):
        shutil.rmtree(stage.parent, ignore_errors=True)
    if package and package.parent.name.startswith('talkman-desktop-download-'):
        shutil.rmtree(package.parent, ignore_errors=True)


def acknowledge_startup():
    value = os.environ.pop('TALKMAN_UPDATE_RECEIPT', '')
    if value:
        path = Path(value)
        if path.name == 'started' and path.parent.name.startswith('talkman-desktop-download-') and path.parent.is_dir():
            try:
                path.touch()
            except OSError:
                pass

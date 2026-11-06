"""按当前构建环境收集实际第三方许可，不复制业务源码或凭证。"""
from pathlib import Path
from importlib import metadata
import re
import shutil
import sys
import hashlib
import io
import json
import tarfile
from urllib.request import urlopen
from urllib.parse import urlsplit

ROOT = Path(__file__).resolve().parent
OUT = ROOT/'TalkMan-assets/legal/third-party'

def collect_source_notices(name, version):
    # 部分 PyObjC wheel 不带许可文件，从同版本官方 PyPI 源码包取原文。
    # 只读取文字、核对 PyPI SHA256，不执行或解压源码。
    with urlopen(f'https://pypi.org/pypi/{name}/{version}/json',timeout=30) as reply:
        info=json.load(reply)
    source=next(x for x in info['urls'] if x['packagetype']=='sdist')
    if urlsplit(source['url']).hostname!='files.pythonhosted.org':
        raise RuntimeError('第三方许可来源不符合预期')
    with urlopen(source['url'],timeout=30) as reply:
        raw=reply.read(64*1024*1024+1)
    if len(raw)>64*1024*1024 or hashlib.sha256(raw).hexdigest()!=source['digests']['sha256']:
        raise RuntimeError('第三方许可源码包校验失败')
    count=0
    with tarfile.open(fileobj=io.BytesIO(raw),mode='r:gz') as archive:
        for member in archive.getmembers():
            path=Path(member.name)
            if not member.isfile() or member.size>2*1024*1024 or '..' in path.parts:
                continue
            if not re.match(r'(?i)^(licen[cs]e|copying|notice)([._-]|$)',path.name):
                continue
            destination=OUT/name/('-'.join(path.parts[1:]))
            destination.parent.mkdir(parents=True,exist_ok=True)
            destination.write_bytes(archive.extractfile(member).read())
            count+=1
    return count

def collect():
    OUT.mkdir(parents=True,exist_ok=True)
    names=['websocket-client','sounddevice','cffi','pycparser','certifi','cryptography',
           'pyinstaller','pyinstaller-hooks-contrib','altgraph','packaging','setuptools']
    names += (['comtypes','pywin32-ctypes','pefile'] if sys.platform=='win32' else
              ['macholib'] + [d.metadata['Name'] for d in metadata.distributions() if d.metadata['Name'].lower().startswith('pyobjc')])
    manifest=[]
    for name in sorted(set(names)):
        dist=metadata.distribution(name)
        prefix=re.sub('[^a-zA-Z0-9._-]','-',name)
        count=0
        for file in dist.files or []:
            relative=Path(file)
            if not any(re.search(r'(?i)^(licen[cs]e|copying|notice|authors|ofl)([._-]|$)',part) for part in relative.parts):
                continue
            source=dist.locate_file(file)
            if not source.is_file() or source.stat().st_size>2*1024*1024:
                continue
            dest=OUT/prefix/Path(*[p for p in relative.parts if p not in ('.','..')])
            dest.parent.mkdir(parents=True,exist_ok=True)
            shutil.copyfile(source,dest)
            count+=1
        if not count and name.lower().startswith('pyobjc'):
            count=collect_source_notices(name,dist.version)
        if not count:
            raise RuntimeError('未找到组件许可原文：'+name)
        manifest.append(f'{name} {dist.version}：{count} 份许可／声明')
    runtime=Path(sys.base_prefix)
    candidates=[runtime/'LICENSE.txt',runtime/'LICENSE',runtime/'Resources/LICENSE.txt']
    for source in candidates:
        if source.is_file():
            shutil.copyfile(source,OUT/'Python-LICENSE.txt')
            break
    else:
        # macOS python.org / CI runtime stores its full PSF license in stdlib.
        text=sys.license if isinstance(getattr(sys,'license',None),str) else None
        import builtins
        license_text=getattr(builtins,'license',None)
        if license_text is not None:
            license_text._Printer__setup()
            text='\n'.join(license_text._Printer__lines)
        if not text or len(text)<100:
            raise RuntimeError('未找到 Python 许可原文')
        (OUT/'Python-LICENSE.txt').write_text(text,encoding='utf-8')
    # Tcl/Tk 二进制分发中的版权原文。
    for directory in (runtime/'tcl',runtime/'lib'):
        if directory.is_dir():
            for source in directory.rglob('license.terms'):
                if 'tcl' in str(source).lower() or 'tk' in str(source).lower():
                    dest=OUT/'Tcl-Tk'/('-'.join(source.relative_to(directory).parts))
                    dest.parent.mkdir(parents=True,exist_ok=True)
                    shutil.copyfile(source,dest)
    (OUT/('TalkMan-依赖版本-'+sys.platform+'.txt')).write_text('\n'.join(manifest)+'\n',encoding='utf-8')
    print('已收集第三方许可：'+str(len(manifest))+' 个组件')

if __name__=='__main__':
    collect()

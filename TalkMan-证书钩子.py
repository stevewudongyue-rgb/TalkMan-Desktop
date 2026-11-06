"""为打包的 Mac 应用提供 CA 证书，保持 HTTPS 和 WSS 验证。"""
import os
import sys

if sys.platform == "darwin":
    import certifi
    os.environ["SSL_CERT_FILE"] = certifi.where()

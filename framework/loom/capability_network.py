"""Exact approved HTTPS requests and wheel extraction; never an admin installer."""
import hashlib
import http.client
import io
import ipaddress
import json
from pathlib import PurePosixPath
import socket
import ssl
import time
from urllib.parse import urlsplit, urljoin, urlencode
import zipfile

from .policy import Denied


def check_url(url):
    if not isinstance(url, str) or len(url) > 3000 or any(ord(c) < 33 for c in url):
        raise ValueError('需要完整、明确的HTTPS地址。')
    p = urlsplit(url)
    if (p.scheme != 'https' or not p.hostname or p.username or p.password
            or p.port not in (None, 443) or p.fragment or '\\' in url):
        raise ValueError('仅接受明确的HTTPS地址，不含账号、片段或自定义端口。')
    return p


def public_address(value):
    return ipaddress.ip_address(value).is_global


def tls_socket(host,address,proxy=None):
    """The trusted proxy only tunnels an already validated literal address."""
    context=ssl.create_default_context();context.set_alpn_protocols(['http/1.1'])
    if proxy:
        p=urlsplit(proxy)
        if p.scheme!='http' or p.hostname!='127.0.0.1' or not p.port or p.username or p.password or p.path not in ('','/') or p.query or p.fragment:
            raise ValueError('Explicit protected loopback proxy required')
        tunnel=http.client.HTTPConnection('127.0.0.1',p.port,timeout=15)
        try:
            tunnel.set_tunnel(address,443);tunnel.connect()
            raw=tunnel.sock;tunnel.sock=None
        finally:tunnel.close()
    else:raw=socket.create_connection((address,443),timeout=15)
    try:return context.wrap_socket(raw,server_hostname=host)
    except BaseException:raw.close();raise


class PublicRoute:
    """A protected route, independent of desktop VPN fake DNS. No user-supplied proxy."""
    def __init__(self,proxy):self.proxy=proxy

    def resolve(self,host,guard):
        try:
            ip=ipaddress.ip_address(host)
        except ValueError:ip=None
        if ip:
            if not ip.is_global:raise Denied('公开网页接口不访问本机、内网或特殊地址。')
            return [str(ip)]
        if '.' not in host or host.lower().endswith(('.localhost','.local','.internal')):
            raise Denied('公开网页接口不查询内部主机名。')
        guard()
        # The resolver itself has a fixed public endpoint and verified TLS name;
        # an intercepted system DNS answer cannot redirect this bootstrap.
        client=http.client.HTTPSConnection('cloudflare-dns.com',timeout=15)
        client.sock=tls_socket('cloudflare-dns.com','1.1.1.1',self.proxy)
        try:
            client.request('GET','/dns-query?'+urlencode({'name':host,'type':'A'}),headers={'Accept':'application/dns-json'})
            response=client.getresponse();raw=response.read(65537)
            if response.status!=200 or len(raw)>65536:raise Denied('公开域名解析未取得有效结果。')
            data=json.loads(raw)
            questions=data.get('Question',[])
            if data.get('Status')!=0 or data.get('TC') or len(questions)!=1 or questions[0].get('name','').rstrip('.').lower()!=host.lower() or questions[0].get('type')!=1:
                raise Denied('公开域名解析结果不匹配或不完整。')
            ips=[a['data'] for a in data.get('Answer',[]) if a.get('type') in (1,28)]
            if not ips or any(not public_address(ip) for ip in ips):raise Denied('公开域名未解析到允许的公网地址。')
            guard();return ips
        finally:client.close()


def request(spec, guard, max_bytes=2*1024*1024, *, accept=None, follow_redirects=0, metadata=False, route=None):
    """Pin the validated public IP, preserving TLS hostname verification.

    Redirects are off by default; the public GET reader may allow bounded hops.
    No proxy inheritance, cookies, injected auth, or arbitrary headers.
    The exact URL/method/body is taken from the approved specification, not call
    arguments. POST is not retried when its delivery/result is unknown.
    """
    p = check_url(spec['url'])
    method = spec.get('method', 'GET')
    if method not in ('GET', 'POST'):
        raise Denied('当前接口只接通GET/POST；其他动作应提出具体接入需求。')
    body = spec.get('body', '').encode('utf-8')
    if len(body) > 256*1024 or (method == 'GET' and body):
        raise ValueError('请求正文不符合已接通接口约定。')
    guard()
    addresses = (route.resolve(p.hostname,guard) if route else
                 [a[4][0] for a in socket.getaddrinfo(p.hostname, 443, type=socket.SOCK_STREAM)])
    if not addresses or any(not public_address(a) for a in addresses):
        raise Denied('公开网页接口不访问本机、内网或特殊地址。')
    address = addresses[0]
    connection = http.client.HTTPSConnection(p.hostname, 443, timeout=15, context=ssl.create_default_context())
    raw = tls_socket(p.hostname,address,route.proxy if route else None)
    try:
        connection.sock = raw
        guard()
        headers={'User-Agent': 'LOOM/2', 'Accept-Encoding': 'identity',
                 'Content-Type': spec.get('content_type', 'application/json')}
        if accept:headers['Accept']=accept
        connection.request(method, (p.path or '/') + ('?'+p.query if p.query else ''),
                           body=body or None, headers=headers)
        response = connection.getresponse()
        if 300 <= response.status < 400:
            if method=='GET' and follow_redirects>0 and response.getheader('Location'):
                url=urljoin(spec['url'],response.getheader('Location'))
                # Every hop repeats URL, public-DNS and TLS validation; no
                # credential/cookie headers are carried into a redirected host.
                next_spec=dict(spec,url=url)
                result=request(next_spec,guard,max_bytes,accept=accept,
                               follow_redirects=follow_redirects-1,metadata=metadata,route=route)
                if metadata:result[2]['redirects'].insert(0,spec['url'])
                return result
            raise Denied('网页发生跳转；新地址应先核对允许范围，未自动跟随。')
        if not 200 <= response.status < 300:
            raise Denied('接口返回HTTP '+str(response.status)+'，未取得成功结果。')
        chunks=[]; size=0; deadline=time.monotonic()+30
        while True:
            guard()
            if time.monotonic() > deadline:
                raise Denied('接口读取超时，结果未确认。')
            chunk=response.read(min(65536,max_bytes-size+1))
            if not chunk:break
            size+=len(chunk)
            if size>max_bytes:raise Denied('返回内容超出本次接入预算。')
            chunks.append(chunk)
        guard()
        result=(b''.join(chunks),response.getheader('Content-Type', ''))
        if metadata:return (*result,{'url':spec['url'],'redirects':[],
                                    'last_modified':response.getheader('Last-Modified')})
        return result
    finally:
        connection.close(); raw.close()


def wheel_files(raw, expected):
    if hashlib.sha256(raw).hexdigest() != expected:
        raise Denied('依赖校验值不一致，未安装。')
    files={}; total=0
    with zipfile.ZipFile(io.BytesIO(raw)) as archive:
        if len(archive.infolist())>160:raise Denied('依赖文件数量超限。')
        for item in archive.infolist():
            from .capabilities import safe_name
            safe_name(item.filename.rstrip('/'))
            p=PurePosixPath(item.filename)
            if (p.is_absolute() or '..' in p.parts or '\\' in item.filename or ':' in item.filename
                    or not p.parts or (item.external_attr >> 16)&0o170000==0o120000):
                raise Denied('依赖含不安全路径或链接。')
            if item.is_dir():continue
            if any(x.endswith('.data') for x in p.parts) or p.suffix.lower() in ('.pth','.exe','.dll','.pyd'):
                raise Denied('此依赖需要额外安装步骤或原生组件；请保留项目并提出接入方案。')
            total+=item.file_size
            if total>16*1024*1024 or item.file_size>4*1024*1024:
                raise Denied('依赖展开预算超限。')
            if str(p).casefold() in {x.casefold() for x in files}:
                raise Denied('依赖含重复文件路径。')
            files[str(p)]=archive.read(item)
    return files

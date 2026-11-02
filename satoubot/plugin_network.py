"""HTTP broker for the isolated provider process.

Resolve and pin public addresses for each request and redirect. Never inherit
system proxy settings; a provider cannot select a different connected host.
"""
import asyncio
import ipaddress
import socket
from urllib.parse import urljoin

import httpx


async def public_address(host):
    addresses = await asyncio.get_running_loop().getaddrinfo(host, None, type=socket.SOCK_STREAM)
    ips = [ipaddress.ip_address(item[4][0].split('%', 1)[0]) for item in addresses]
    # Reject address formats that can tunnel or translate to private IPv4.
    nat64 = (ipaddress.ip_network('64:ff9b::/96'), ipaddress.ip_network('64:ff9b:1::/48'))
    def allowed(ip):
        if not ip.is_global or ip.is_reserved: return False
        if isinstance(ip, ipaddress.IPv6Address):
            return not (ip.ipv4_mapped or ip.sixtofour or ip.teredo or any(ip in block for block in nat64))
        return True
    if not ips or not all(allowed(ip) for ip in ips):
        raise ValueError('Destination is not public')
    return str(ips[0])


async def fetch_public(url, method='GET', headers=None, body=None):
    if method not in ('GET', 'POST', 'HEAD') or not isinstance(url, str) or len(url) > 8192:
        raise ValueError('Unsupported request')
    if body is not None and (not isinstance(body, str) or len(body.encode()) > 1_000_000):
        raise ValueError('Request body too large')
    if not isinstance(headers, dict) or len(headers) > 40:
        raise ValueError('Invalid headers')
    clean = {}
    for k, v in headers.items():
        if not isinstance(k, str) or not isinstance(v, str) or len(k) > 100 or len(v) > 8192:
            raise ValueError('Invalid header')
        if k.lower() not in ('host', 'connection', 'proxy-authorization', 'content-length', 'transfer-encoding'):
            clean[k] = v
    async with httpx.AsyncClient(trust_env=False, follow_redirects=False, timeout=20) as client:
        for redirect in range(6):
            parsed = httpx.URL(url)
            if parsed.scheme not in ('http', 'https') or not parsed.host or parsed.userinfo:
                raise ValueError('Invalid URL')
            ip = await public_address(parsed.host)
            target = parsed.copy_with(host=ip)
            request_headers = {**clean, 'Host': parsed.netloc.decode('ascii')}
            async with client.stream(method, target, headers=request_headers, content=body,
                                     extensions={'sni_hostname': parsed.host}) as response:
                if response.status_code in (301, 302, 303, 307, 308) and response.headers.get('location'):
                    if redirect == 5: raise ValueError('Too many redirects')
                    next_url = urljoin(str(parsed), response.headers['location'])
                    next_parsed = httpx.URL(next_url)
                    if (next_parsed.scheme, next_parsed.host, next_parsed.port) != (parsed.scheme, parsed.host, parsed.port):
                        clean = {k: v for k, v in clean.items() if k.lower() not in ('authorization', 'cookie')}
                    if response.status_code == 303 or (response.status_code in (301, 302) and method == 'POST'):
                        method, body = 'GET', None
                    url = next_url
                    continue
                data = bytearray()
                async for chunk in response.aiter_bytes():
                    data.extend(chunk)
                    if len(data) > 8_000_000: raise ValueError('Response too large')
                return {'status': response.status_code, 'statusText': response.reason_phrase,
                        'url': str(parsed), 'headers': dict(response.headers),
                        'body': bytes(data).decode('utf-8', errors='replace')}
    raise ValueError('Request failed')

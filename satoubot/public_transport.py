"""Resolve and pin each media request, including redirects, to a public IP."""
import httpx
from .plugin_network import public_address


class PublicTransport(httpx.AsyncBaseTransport):
    def __init__(self):
        self.transport=httpx.AsyncHTTPTransport(trust_env=False, limits=httpx.Limits(max_keepalive_connections=0))

    async def handle_async_request(self, request):
        url=request.url
        if url.scheme not in ('http','https') or not url.host or url.userinfo:
            raise ValueError('Unsafe media URL')
        ip=await public_address(url.host)
        headers=httpx.Headers(request.headers)
        headers['host']=url.netloc.decode('ascii')
        extensions=dict(request.extensions)
        extensions['sni_hostname']=url.host
        pinned=httpx.Request(request.method,url.copy_with(host=ip),headers=headers,stream=request.stream,extensions=extensions)
        return await self.transport.handle_async_request(pinned)

    async def aclose(self):
        await self.transport.aclose()

"""Resolve catalogue identities without matching titles heuristically."""
import re
from urllib.parse import quote, urljoin


def supports_resource(manifest, resource_name, media_type, item_id):
    for resource in manifest.get('resources', []):
        if isinstance(resource, str):
            name, types, prefixes = resource, manifest.get('types', []), manifest.get('idPrefixes', [])
        elif isinstance(resource, dict):
            name = resource.get('name')
            types = resource.get('types') or manifest.get('types', [])
            prefixes = resource.get('idPrefixes') or manifest.get('idPrefixes', [])
        else: continue
        if name == resource_name and (not types or media_type in types) and (not prefixes or any(item_id.startswith(p) for p in prefixes)):
            return True
    return False


def tmdb_identity(movie):
    for value in (movie.get('moviedb_id'), movie.get('tmdb_id'), (movie.get('external_ids') or {}).get('tmdb_id')):
        if value is not None and re.fullmatch(r'[1-9]\d*', str(value)):
            return str(value)
    identity = str(movie.get('id', ''))
    for prefix in ('tmdb:', 'movie:', 'series:'):
        if identity.startswith(prefix): identity = identity[len(prefix):]; break
    if re.fullmatch(r'[1-9]\d*', identity): return identity
    return None


def imdb_identity(movie):
    for value in (movie.get('imdb_id'), movie.get('imdbId'), (movie.get('external_ids') or {}).get('imdb_id'), movie.get('id')):
        match = re.fullmatch(r'(tt\d+)(?::\d+:\d+)?', str(value or ''))
        if match: return match.group(1)
    return None


async def resolve_tmdb(service, movie):
    direct = tmdb_identity(movie)
    if direct:
        return direct
    item_id = str(movie.get('id') or '')
    if not item_id:
        raise ValueError('No catalogue identifier is available for this title.')
    media_type = movie.get('type', 'movie')
    # Ask only explicitly enabled metadata add-ons for the same catalogue ID.
    # Never identify a different title by name or guess at unrelated identifiers.
    for url, kind, manifest in service.manifests():
        if kind != 'addon' or not supports_resource(manifest, 'meta', media_type, item_id):
            continue
        try:
            endpoint = f'meta/{quote(media_type, safe="")}/{quote(item_id, safe="")}.json'
            data = await service.cached_json(urljoin(url, endpoint))
            meta = data.get('meta')
            if not isinstance(meta, dict):
                continue
            if meta.get('id') is not None and str(meta['id']) != item_id:
                continue
            resolved = tmdb_identity({k: v for k, v in meta.items() if k != 'id'})
            if resolved:
                return resolved
        except Exception:
            continue
    raise ValueError('Enabled catalogues did not provide a TMDB ID for this title. Add or enable a compatible catalogue; stream add-ons may still return files.')

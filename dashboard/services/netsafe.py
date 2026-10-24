"""Outbound-request safety for the one place the server fetches a user-supplied address (a property's iCal link).

Without this a logged-in user could point the server at http://169.254.169.254/ (the cloud metadata service), at localhost, or at a
file:// path. Only https to a PUBLIC address is allowed, every redirect is checked again, and the connection goes to the address
that was checked (so a DNS answer that changes between the check and the request cannot swap in a private one)."""
import http.client
import ipaddress
import socket
import ssl
import urllib.parse

MAX_BYTES = 5 * 1024 * 1024
MAX_REDIRECTS = 3


def _public(ip):
    a = ipaddress.ip_address(ip)
    return a.is_global and not a.is_multicast


def resolve_public(host, port=443):
    """The public IPs `host` resolves to. ValueError (in plain words) if it resolves to nothing, or to ANY non-public address."""
    try:
        infos = socket.getaddrinfo(host, port, type=socket.SOCK_STREAM)
    except socket.gaierror:
        raise ValueError("That address could not be found.")
    ips = sorted({i[4][0] for i in infos})
    if not ips or not all(_public(ip) for ip in ips):
        raise ValueError("That address points inside a private or internal network, which isn't allowed. Use the calendar's public export link.")
    return ips


def check_url(url):
    """Parse and validate a calendar link; returns (host, port, path_with_query, ips)."""
    u = urllib.parse.urlsplit((url or "").strip())
    if u.scheme != "https":
        raise ValueError("Only secure https:// calendar links are allowed.")
    if not u.hostname or u.username or u.password:
        raise ValueError("That isn't a valid calendar link.")
    port = u.port or 443
    if port != 443:
        raise ValueError("Only standard https links (port 443) are allowed.")
    try:
        literal = ipaddress.ip_address(u.hostname)
    except ValueError:
        literal = None
    ips = [str(literal)] if literal else resolve_public(u.hostname, port)
    if literal and not _public(str(literal)):
        raise ValueError("That address points inside a private or internal network, which isn't allowed. Use the calendar's public export link.")
    return u.hostname, port, (u.path or "/") + (("?" + u.query) if u.query else ""), ips


class _PinnedHTTPS(http.client.HTTPSConnection):
    """HTTPS to a specific, already-validated IP while still verifying the certificate for the real hostname."""
    def __init__(self, host, ip, port, timeout):
        super().__init__(host, port, timeout=timeout, context=ssl.create_default_context())
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, self.port), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def fetch_public_https(url, timeout=15, user_agent="Mozilla/5.0 (compatible; PropertyAnalyticsDashboard/1.0)"):
    """GET a public https URL (following a few redirects, re-checking each) and return the body text."""
    for _ in range(MAX_REDIRECTS + 1):
        host, port, path, ips = check_url(url)
        conn = _PinnedHTTPS(host, ips[0], port, timeout)
        try:
            conn.request("GET", path, headers={"Host": host, "User-Agent": user_agent, "Accept": "text/calendar, text/plain, */*"})
            resp = conn.getresponse()
            if resp.status in (301, 302, 303, 307, 308):
                loc = resp.getheader("Location")
                if not loc:
                    raise ValueError("The calendar link redirected nowhere.")
                url = urllib.parse.urljoin(url, loc)
                continue
            if resp.status != 200:
                raise HTTPStatus(resp.status)
            body = resp.read(MAX_BYTES + 1)
            if len(body) > MAX_BYTES:
                raise ValueError("That calendar file is far larger than a normal calendar, so it was not read.")
            return body.decode("utf-8", errors="replace")
        except (socket.timeout, TimeoutError):
            raise ValueError("Timed out reaching that calendar URL.")
        except (OSError, http.client.HTTPException) as e:
            if isinstance(e, ssl.SSLError):
                raise ValueError("That site's security certificate could not be verified.")
            raise ValueError(f"Couldn't reach that calendar URL: {e.__class__.__name__}")
        finally:
            conn.close()
    raise ValueError("The calendar link redirected too many times.")


class HTTPStatus(ValueError):
    def __init__(self, code):
        super().__init__(f"The calendar URL returned an error ({code}) -- double check it's the right export link.")
        self.code = code

"""Bounded disposal of a rejected POST body; never parses or authorizes it."""
import time

MAX_REJECTED_BODY=150000
REJECTED_BODY_SECONDS=.25
DISCARD_CHUNK=8192


def drain_rejected_post(handler):
    """Avoid closing on an ordinary body still arriving after its headers.

    Only unambiguous small fixed-length POSTs qualify. Framing ambiguity,
    Expect/transfer encoding, oversized input, EOF or the total deadline cause
    immediate connection closure. Contents are discarded without logging.
    """
    if handler.command!='POST':return False
    handler.close_connection=True
    if getattr(handler,'_request_body_consumed',False):return True
    if getattr(handler,'_rejected_body_drained',False):return False
    handler._rejected_body_drained=True
    headers=handler.headers
    if headers.get_all('Transfer-Encoding',[]) or headers.get_all('Expect',[]):return False
    lengths=headers.get_all('Content-Length',[])
    if len(lengths)!=1 or not isinstance(lengths[0],str):return False
    value=lengths[0].strip(' \t')
    if not value or not value.isascii() or not value.isdigit():return False
    normalized=value.lstrip('0') or '0'
    if len(normalized)>6:return False
    remaining=int(normalized)
    if remaining>MAX_REJECTED_BODY:return False
    if not remaining:
        handler._request_body_consumed=True
        return True
    # read1 performs at most one raw read instead of waiting for a full chunk;
    # re-check the total deadline even if a sender trickles individual bytes.
    read=getattr(handler.rfile,'read1',None)
    if not callable(read):return False
    connection=handler.connection
    timeout=connection.gettimeout()
    deadline=time.monotonic()+REJECTED_BODY_SECONDS
    try:
        while remaining:
            wait=deadline-time.monotonic()
            if wait<=0:return False
            connection.settimeout(wait)
            size=min(remaining,DISCARD_CHUNK)
            chunk=read(size)
            if not isinstance(chunk,(bytes,bytearray)) or not chunk or len(chunk)>size:return False
            remaining-=len(chunk)
        handler._request_body_consumed=True
        return True
    except (OSError,ValueError):
        return False
    finally:
        try:connection.settimeout(timeout)
        except OSError:pass

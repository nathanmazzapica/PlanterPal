import urequests
import errno
from app.state import State
from web.wifi_config import cfg
from web.exceptions import ErrNetwork, ErrHostUnreachable, ErrTimedOut, ErrConnectionReset

def ping():
    url = f"http://{cfg['host']}/healthz"
    response = urequests.get(url)
    print("Status code:", response.status_code)
    response.close()
    return response.status_code

def report(state: State):
    url = f"http://{cfg['host']}/api/v1/readings"
    
    try:
        response = urequests.post(url, headers= {"content-type": 'application/json'}, 
                              data = state.to_json())
    except OSError as ose:
        if ose.args[0] == errno.EHOSTUNREACH:
            raise ErrHostUnreachable
        if ose.args[0] == errno.ETIMEDOUT:
            raise ErrTimedOut
        if ose.errno == errno.ECONNRESET:
            raise ErrConnectionReset
        raise ErrNetwork

    return response.status_code
    

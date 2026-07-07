import urequests
from app.state import State
from web.wifi_config import cfg

def ping():
    url = f"http://{cfg['host']}/healthz"
    response = urequests.get(url)
    print("Status code:", response.status_code)
    response.close()
    return response.status_code

def report(state: State):
    url = f"http://{cfg['host']}/api/v1/readings"
    response = urequests.post(url, headers= {"content-type": 'application/json'}, 
                              data = state.to_json())
    return response.status_code
    

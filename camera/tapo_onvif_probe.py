"""Raw ONVIF PullPoint probe for the Tapo C325WB (zeep chokes on its long-poll). Usage: TAPO_PASS=... python3 tapo_onvif_probe.py SECONDS"""
import base64, hashlib, os, re, sys, time, datetime, urllib.request
CAM=os.environ.get("TAPO_HOST","192.168.4.110"); U=os.environ.get("TAPO_USER","glados-ops"); P=os.environ["TAPO_PASS"]
def sec():
    nonce=os.urandom(16); created=datetime.datetime.utcnow().strftime("%Y-%m-%dT%H:%M:%S.000Z")
    dig=base64.b64encode(hashlib.sha1(nonce+created.encode()+P.encode()).digest()).decode()
    return f'''<wsse:Security xmlns:wsse="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-secext-1.0.xsd" xmlns:wsu="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-wssecurity-utility-1.0.xsd"><wsse:UsernameToken><wsse:Username>{U}</wsse:Username><wsse:Password Type="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-username-token-profile-1.0#PasswordDigest">{dig}</wsse:Password><wsse:Nonce EncodingType="http://docs.oasis-open.org/wss/2004/01/oasis-200401-wss-soap-message-security-1.0#Base64Binary">{base64.b64encode(nonce).decode()}</wsse:Nonce><wsu:Created>{created}</wsu:Created></wsse:UsernameToken></wsse:Security>'''
def soap(url, action, body, to=None):
    hdr = sec() + (f'<wsa:Action>{action}</wsa:Action><wsa:To>{to}</wsa:To>' if to else '')
    env=f'''<?xml version="1.0" encoding="UTF-8"?><s:Envelope xmlns:s="http://www.w3.org/2003/05/soap-envelope" xmlns:wsa="http://www.w3.org/2005/08/addressing" xmlns:tev="http://www.onvif.org/ver10/events/wsdl"><s:Header>{hdr}</s:Header><s:Body>{body}</s:Body></s:Envelope>'''
    req=urllib.request.Request(url, env.encode(), {"Content-Type":f'application/soap+xml; charset=utf-8; action="{action}"'})
    return urllib.request.urlopen(req, timeout=15).read().decode()
r=soap(f"http://{CAM}:2020/onvif/service","http://www.onvif.org/ver10/events/wsdl/EventPortType/CreatePullPointSubscriptionRequest",
       '<tev:CreatePullPointSubscription><tev:InitialTerminationTime>PT600S</tev:InitialTerminationTime></tev:CreatePullPointSubscription>')
addr=re.search(r"<wsa\d*:Address>([^<]+)<",r) or re.search(r"Address>([^<]+)<",r)
addr=addr.group(1); print("sub", addr, flush=True)
end=time.time()+float(sys.argv[1]); n=0
while time.time()<end:
    try:
        r=soap(addr,"http://www.onvif.org/ver10/events/wsdl/PullPointSubscription/PullMessagesRequest",
               '<tev:PullMessages><tev:Timeout>PT5S</tev:Timeout><tev:MessageLimit>20</tev:MessageLimit></tev:PullMessages>', to=addr)
    except Exception as e:
        print("err", e, flush=True); time.sleep(1); continue
    for m in re.findall(r"<wsnt:NotificationMessage>(.*?)</wsnt:NotificationMessage>", r, re.S):
        n+=1
        topic=re.search(r"<wsnt:Topic[^>]*>([^<]+)<",m)
        items=re.findall(r'Name="([^"]+)"\s+Value="([^"]+)"', m)
        if n<=2: print("RAW:", re.sub(r"\s+"," ",m)[:900], flush=True)
        print(time.strftime("%H:%M:%S"), topic.group(1) if topic else "?", items, flush=True)
print("messages", n)

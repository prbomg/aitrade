"""Verified TLS, no secrets in argv, no automatic POST replay."""
import hashlib,hmac,json,time,urllib.request,urllib.parse,os
class ExchangeError(RuntimeError):
    def __init__(self, message, code=None, uncertain=False):
        super().__init__(message);self.code=code;self.uncertain=uncertain

def request(base_url, key, secret, method, path, params, timeout=8):
    if method=='POST' and os.environ.get('AITRADE_READ_ONLY')=='1':raise ExchangeError('Read-only validation mode')
    if not key or not secret:raise ExchangeError('Биржевые ключи не настроены')
    if base_url not in ('https://api-demo.bybit.com','https://api.bybit.com'):raise ExchangeError('Недопустимый адрес биржи')
    ts=str(int(time.time()*1000));recv='10000'
    payload=urllib.parse.urlencode(sorted(params.items())) if method=='GET' else json.dumps(params,separators=(',',':'))
    sign=hmac.new(secret.encode(),(ts+key+recv+payload).encode(),hashlib.sha256).hexdigest()
    url=base_url+path+('?' + payload if method=='GET' and payload else '')
    headers={'Content-Type':'application/json','X-BAPI-API-KEY':key,'X-BAPI-TIMESTAMP':ts,'X-BAPI-RECV-WINDOW':recv,'X-BAPI-SIGN':sign}
    try:
        with urllib.request.urlopen(urllib.request.Request(url,data=payload.encode() if method=='POST' else None,headers=headers,method=method),timeout=timeout) as r: data=json.load(r)
    except Exception as e:raise ExchangeError('Ошибка связи с Bybit: '+type(e).__name__,uncertain=method=='POST') from None
    if not isinstance(data,dict) or data.get('retCode')!=0:
        code=data.get('retCode') if isinstance(data,dict) else None
        if path.endswith('/set-leverage') and code==110043:return dict(data,retCode=0)
        raise ExchangeError('Bybit '+str(code)+': '+str(data.get('retMsg','Некорректный ответ'))[:180],code=code)
    return data

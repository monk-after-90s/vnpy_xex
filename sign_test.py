import hashlib
import hmac
import time
import urllib
from urllib.parse import urljoin
import beeprint


def sign(path: str = "/api/v3/order", headers: dict = None, params: dict = None, data: dict = None):
    """生成XEX签名"""
    if params is None: params = {}
    params['timestamp'] = int(time.time() * 1000)
    params['recvWindow'] = 50000
    # 计算签名
    query = str_to_sign = urllib.parse.urlencode(params)
    if data:
        str_to_sign += json.dumps(data)
    global secret
    signature = \
        hmac.new(secret.encode(), str_to_sign.encode("utf-8"), hashlib.sha256).hexdigest()
    query += f"&signature={signature}"
    # query取代params
    path += "?" + query
    # 添加请求头
    if headers is None: headers = {}
    headers["X-MBX-APIKEY"] = apikey
    return headers, path


def simple_sign(params: dict):
    query = urllib.parse.urlencode(sorted(params.items()))
    sign = hmac.new(secret.encode(), query.encode("utf-8"), hashlib.sha256).hexdigest()
    return sign


if __name__ == '__main__':
    import requests
    import json

    apikey = "7f943fb10c73ea37c20b044448ebf9be0bfc4eeda1dc6dce85ec3c635824924b"
    secret = "34d5ab4ccdae77203095417d927cf8196424bbba48480d2c6237aeb84681d934"
    base_url = "http://openapi.hipiex.net/spot/"
    # 获取用户资金
    # path = "v1/u/wallet/list"
    # params = {"coin": "USDT"}
    # signature = simple_sign(params)
    # headers = {
    #     "x_access_key": apikey,
    #     "x_signature": signature,
    #     'Content-Type': 'application/json',
    # }
    # wallet_url = urljoin(base_url, path)
    # response = requests.request("GET", wallet_url, headers=headers, params=params)
    # beeprint.pp("wallet:")
    # beeprint.pp(response.json())
    # 查询未完成订单
    path = "v1/trade/order/listUnfinished"
    params = {"symbol": "ETH_USDT", "direction": "BUY"}
    signature = simple_sign(params)
    headers = {
        "x_access_key": apikey,
        "x_signature": signature,
        'Content-Type': 'application/json',
    }
    full_url = urljoin(base_url, path)
    response = requests.request("GET", full_url, headers=headers, params=params)
    beeprint.pp("listUnfinished:")
    beeprint.pp(response.json())
    # 开单
    path = 'v1/trade/order/create'
    params = {}
    request_body = {"symbol": "LTC_USDT", "price": 100, "amount": 1, "direction": "BUY", "orderType": "LIMIT"}
    signature = hmac.new(secret.encode(), ("#" + json.dumps(request_body)).encode("utf-8"), hashlib.sha256).hexdigest()
    headers = {
        "x_access_key": apikey,
        "x_signature": signature,
        'Content-Type': 'application/json',
    }
    wallet_url = urljoin(base_url, path)
    response = requests.request("POST", wallet_url, headers=headers, data=request_body)
    beeprint.pp("order:")
    beeprint.pp(response.json())

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
    headers["x_access_key"] = apikey
    headers["Content-Type"] = "application/json"
    return headers, path


def simple_sign(params: dict):
    query = urllib.parse.urlencode(sorted(params.items()))
    sign = hmac.new(secret.encode(), query.encode(), hashlib.sha256).hexdigest()
    return sign


if __name__ == '__main__':
    import requests
    import json

    apikey = "76fa9f402f690fd21f1cac76fac95ec100cf6af0eec4d2df327eb85575c09427"
    secret = "d489f7d00692e0077aa62a09c609f7daee6f84645fa96d245834817fedbef818"
    # apikey = "7f943fb10c73ea37c20b044448ebf9be0bfc4eeda1dc6dce85ec3c635824924b"
    # secret = "34d5ab4ccdae77203095417d927cf8196424bbba48480d2c6237aeb84681d934"
    base_url = "https://openapi.hipiex.net/spot/"
    # 获取用户资金
    path = "v1/u/wallet/list"
    params = {"coin": "USDT"}
    signature = simple_sign(params)
    headers = {
        "x_access_key": apikey,
        "x_signature": signature,
        'Content-Type': 'application/json',
    }
    wallet_url = urljoin(base_url, path)
    response = requests.request("GET", wallet_url, headers=headers, params=params)
    beeprint.pp("wallet:")
    beeprint.pp(response.json())
    # 查询未完成订单
    path = "v1/trade/order/listUnfinished"
    params = {"symbol": "LTC_USDT", "direction": "BUY"}
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
    # path = 'v1/trade/order/create'
    # request_body = {"symbol": "LTC_USDT",
    #                 "price": "101",
    #                 "amount": "0.2",
    #                 "direction": "BUY",
    #                 "orderType": "LIMIT",
    #                 "clientOrderId": 123456}
    # signature = simple_sign(request_body)
    # headers = {
    #     "x_access_key": apikey,
    #     "x_signature": signature,
    #     'Content-Type': 'application/json',
    # }
    # wallet_url = urljoin(base_url, path + "?" + urllib.parse.urlencode(sorted(request_body.items())))
    # response = requests.post(wallet_url, headers=headers)
    # beeprint.pp("order:")
    # beeprint.pp(response.json())
    # 批量撤单
    # path = "spot/v1/trade/order/batchOrder"
    # params = {"list": '[{"isCreate": False, "symbol": "LTC_USDT", "clientOrderId": 123456}]'}
    # headers = {
    #     "x_access_key": apikey,
    #     "x_signature": simple_sign(params),
    #     'Content-Type': 'application/json',
    # }
    # wallet_url = urljoin(base_url, path + "?" + urllib.parse.urlencode(sorted(params.items())))
    # response = requests.post(wallet_url, headers=headers, params=params, json=params)
    # beeprint.pp("batchCancel:")
    # beeprint.pp(response.json())

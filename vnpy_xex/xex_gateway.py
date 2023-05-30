import asyncio
import hashlib
import hmac
import json
import time
import urllib
from asyncio import run_coroutine_threadsafe
from copy import copy
from datetime import datetime, timedelta
from enum import Enum
from threading import Lock
from vnpy_websocket import WebsocketClient
import pytz
from typing import Any, Dict, List
from requests.exceptions import SSLError
from vnpy.trader.constant import (
    Direction,
    Exchange,
    Product,
    Status,
    OrderType,
    Interval
)
from vnpy.trader.gateway import BaseGateway
from vnpy.trader.object import (
    OrderData,
    AccountData,
    ContractData,
    BarData,
    OrderRequest,
    CancelRequest,
    HistoryRequest,
    SubscribeRequest, TradeData
)
from vnpy.event import EventEngine
from vnpy_rest import RestClient, Request
from vnpy.trader.utility import round_to

# 中国时区
CHINA_TZ = pytz.timezone("Asia/Shanghai")

# 实盘REST API地址
# BASE_URL: str = "http://54.254.54.220:8069/"
BASE_URL: str = "https://openapi.hipiex.net/spot/"

# 实盘Websocket API地址
WEBSOCKET_TRADE_HOST: str = "wss://openapi.hipiex.net/websocket"
# 委托状态映射
STATUS_XEX2VT: Dict[str, Status] = {
    "NEW": Status.NOTTRADED,
    "PARTIALLY_FILLED": Status.PARTTRADED,
    "PARTIALLY_CANCELED": Status.NOTTRADED,
    "FILLED": Status.ALLTRADED,
    "CANCELED": Status.CANCELLED,
    "REJECTED": Status.REJECTED,
    "EXPIRED": Status.CANCELLED
}
STATUS_INT_XEX2VT = {1: Status.NOTTRADED,
                     2: Status.PARTTRADED,
                     3: Status.ALLTRADED,
                     4: Status.CANCELLED,
                     5: Status.REJECTED,
                     6: Status.CANCELLED}
# 委托类型映射
ORDERTYPE_VT2XEX: Dict[OrderType, str] = {
    OrderType.LIMIT: "LIMIT",
    OrderType.MARKET: "MARKET"
}
ORDERTYPE_XEX2VT: Dict[str, OrderType] = {v: k for k, v in ORDERTYPE_VT2XEX.items()}
ORDERTYPE_INT_XEX2VT = {1: OrderType.LIMIT, 2: OrderType.MARKET}

# 买卖方向映射
DIRECTION_VT2XEX: Dict[Direction, str] = {
    Direction.LONG: "BUY",
    Direction.SHORT: "SELL"
}
DIRECTION_XEX2VT: Dict[str, Direction] = {v: k for k, v in DIRECTION_VT2XEX.items()}
DIRECTION_INT_XEX2VT = {1: Direction.LONG, 2: Direction.SHORT}
# 数据频率映射
INTERVAL_VT2XEX: Dict[Interval, str] = {
    Interval.MINUTE: "1m",
    Interval.HOUR: "1h",
    Interval.DAILY: "1d",
}

# 时间间隔映射
TIMEDELTA_MAP: Dict[Interval, timedelta] = {
    Interval.MINUTE: timedelta(minutes=1),
    Interval.HOUR: timedelta(hours=1),
    Interval.DAILY: timedelta(days=1),
}

# 合约数据全局缓存字典
symbol_contract_map: Dict[str, ContractData] = {}


# 鉴权类型
class Security(Enum):
    NONE = 0
    SIGNED = 1


class XEXSpotGateway(BaseGateway):
    """
    vn.py用于对接币XEX货账户的交易接口。
    """

    default_name: str = "XEX_SPOT"

    default_setting: Dict[str, Any] = {
        "key": "",
        "secret": "",
        "代理地址": "",
        "代理端口": 0
    }

    exchanges: Exchange = [Exchange.XEX]

    def __init__(self, event_engine: EventEngine, gateway_name: str) -> None:
        """构造函数"""
        super().__init__(event_engine, gateway_name)

        self.trade_ws_api: "XEXSpotTradeWebsocketApi" = XEXSpotTradeWebsocketApi(self)
        self.rest_api: "XEXSpotRestAPi" = XEXSpotRestAPi(self)

        self.orders: Dict[str, OrderData] = {}
        # 订单号(XEX的orderId)到vntrade OrderData的映射
        self.order_id_map: Dict[str, OrderData] = {}

    def connect(self, setting: dict):
        """连接交易接口"""
        key: str = setting["key"]
        secret: str = setting["secret"]
        proxy_host: str = setting["代理地址"]
        proxy_port: int = setting["代理端口"]

        self.rest_api.connect(key, secret, proxy_host, proxy_port)

    def send_order(self, *reqs: OrderRequest) -> str:
        """委托下单 批量下单"""
        return self.rest_api.send_order(*reqs)

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        self.rest_api.cancel_order(req)

    def query_account(self) -> None:
        """查询资金"""
        pass

    def subscribe(self, req: SubscribeRequest) -> None:
        """订阅行情"""
        pass

    def query_position(self) -> None:
        """查询持仓"""
        pass

    def query_history(self, req: HistoryRequest) -> List[BarData]:
        """查询历史数据"""
        pass

    def close(self) -> None:
        """关闭连接"""
        self.rest_api.stop()
        self.trade_ws_api.stop()

    def on_order(self, order: OrderData) -> None:
        """推送委托数据"""
        self.orders[order.orderid] = copy(order)
        origin_orderId = getattr(order, "origin_orderId", None)
        if origin_orderId is not None:
            self.order_id_map[origin_orderId] = order
        if origin_orderId in self.order_id_map.keys() and (
                order.status == Status.ALLTRADED or order.status == Status.REJECTED or order.status == Status.CANCELLED):
            self.order_id_map.pop(origin_orderId)

        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData:
        """查询委托数据"""
        return self.orders.get(orderid, None)

    @staticmethod
    def vn_symbol_to_exchange_symbol(vn_symbol: str):
        """vnpy的symbol转换为交易所的symbol"""
        assert "." not in vn_symbol
        return vn_symbol


class XEXSpotRestAPi(RestClient):
    """币安现货REST API"""

    def __init__(self, gateway: XEXSpotGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: XEXSpotGateway = gateway
        self.gateway_name: str = gateway.gateway_name

        self.trade_ws_api: XEXSpotTradeWebsocketApi = self.gateway.trade_ws_api

        self.key: str = ""
        self.secret: bytes = b""
        self.proxy_host = ""
        self.proxy_port = ""

        self.user_stream_key: str = ""
        self.keep_alive_count: int = 0
        self.recv_window: int = 5000

        self.order_count: int = 1_000_000
        self.order_count_lock: Lock = Lock()
        self.connect_time: int = 0

    def sign(self, request: Request) -> Request:
        """生成XEX签名"""
        security: Security = request.data["security"]
        request.data.pop("security")
        if security == Security.SIGNED:
            if request.params is None: request.params = {}
            query = ''
            for key, value in sorted(request.params.items()):
                query += f"{key}={value}&"
            query = query[:-1]
            signature = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()
            # 添加请求头
            headers = {
                "x_access_key": self.key,
                "x_signature": signature,
                'Content-Type': 'application/json',
            }
            if request.headers is None: request.headers = {}
            request.headers.update(headers)
        return request

    def connect(
            self,
            key: str,
            secret: str,
            proxy_host: str,
            proxy_port: int,
    ) -> None:
        """连接REST服务器"""
        self.key = key
        self.secret = secret.encode()
        self.proxy_host = proxy_host
        self.proxy_port = proxy_port

        self.connect_time = (
                int(datetime.now(CHINA_TZ).strftime("%y%m%d%H%M%S")) * self.order_count
        )

        self.init(BASE_URL, proxy_host, proxy_port)

        self.start()

        self.gateway.write_log("REST API启动成功")

        self.query_time()
        self.query_account()
        self.query_contract()
        self.start_user_stream()

    def query_order(self) -> None:
        """查询未成交委托"""
        for symbol in symbol_contract_map.keys():
            self.add_request(
                method="GET",
                path="v1/trade/order/listUnfinished",
                params={"symbol": symbol, "direction": "BUY"},
                callback=self.on_query_order,
                data={"security": Security.SIGNED}
            )
            self.add_request(
                method="GET",
                path="v1/trade/order/listUnfinished",
                params={"symbol": symbol, "direction": "SELL"},
                callback=self.on_query_order,
                data={"security": Security.SIGNED}
            )

    def on_query_order(self, data: dict, request: Request) -> None:
        """未成交委托查询回报"""
        if data['code'] == 0:
            for d in data['data']:
                if d['orderType'] not in ORDERTYPE_XEX2VT.keys():
                    continue
                order: OrderData = OrderData(
                    orderid=d['clientOrderId'],
                    symbol=d['symbol'],
                    exchange=Exchange.XEX,
                    price=float(d["price"]),
                    volume=float(d['origQty']),
                    type=ORDERTYPE_XEX2VT[d['orderType']],
                    direction=DIRECTION_XEX2VT[d['orderSide']],
                    traded=float(d['executedQty']),
                    status=STATUS_XEX2VT.get(d['state'], None),
                    datetime=generate_datetime(d['createdTime']),
                    gateway_name=self.gateway_name,
                )
                setattr(order, "origin_orderId", d['orderId'])
                self.gateway.on_order(order)
            if data['data']:
                self.gateway.write_log("委托信息查询成功")

    def query_time(self) -> None:
        """查询时间"""
        ...

    def query_account(self) -> None:
        """查询资金"""
        data: dict = {"security": Security.SIGNED}

        self.add_request(
            method="GET",
            path="v1/u/wallet/list",
            callback=self.on_query_account,
            data=data
        )

    def query_contract(self) -> None:
        """查询合约信息"""
        data: dict = {
            "security": Security.NONE
        }
        self.add_request(
            method="GET",
            path="v1/exchangeInfo",
            callback=self.on_query_contract,
            data=data
        )

    def _new_order_id(self) -> int:
        """生成本地委托号"""
        with self.order_count_lock:
            self.order_count += 1
            return self.order_count

    def send_order(self, *reqs: OrderRequest) -> str:
        """委托下单 批量下单"""
        vt_orderids = []  # 单号
        order_params = []  # 下单参数
        for req in reqs:
            # 生成本地委托号
            orderid: str = str(self.connect_time + self._new_order_id())

            # 推送提交中事件
            order: OrderData = req.create_order_data(
                orderid,
                self.gateway_name
            )
            self.gateway.on_order(order)
            order_params.append({"isCreate": True,
                                 "symbol": req.symbol,
                                 "price": req.price,
                                 "totalAmount": format(req.volume, ".5f"),
                                 "tradeType": ORDERTYPE_VT2XEX[req.type],
                                 "direction": DIRECTION_VT2XEX[req.direction],
                                 "clientOrderId": orderid})
            vt_orderids.append(order.vt_orderid)
        # 批量下单请求
        data: dict = {
            "security": Security.SIGNED
        }
        params: dict = {"list": json.dumps(order_params)}

        self.add_request(
            method="POST",
            path="v1/trade/order/batchOrder",
            callback=self.on_send_order,
            params=params,
            data=data,
            on_error=self.on_send_order_error,
            on_failed=self.on_send_order_failed
        )

        # 生成委托请求
        if len(vt_orderids) == 1:
            return vt_orderids[0]
        else:
            return vt_orderids

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        data: dict = {
            "security": Security.SIGNED
        }

        order: OrderData = self.gateway.get_order(req.orderid)
        params: dict = {"list": json.dumps([{"isCreate": False,
                                             "symbol": self.gateway.vn_symbol_to_exchange_symbol(req.symbol),
                                             'clientOrderId': req.orderid}])}

        self.add_request(
            method="POST",
            path="v1/trade/order/batchOrder",
            callback=self.on_cancel_order,
            params=params,
            data=data,
            on_failed=self.on_cancel_failed,
            extra=order
        )

    def start_user_stream(self):
        """开启账户信息推送"""
        self.trade_ws_api.connect(WEBSOCKET_TRADE_HOST, self.proxy_host, self.proxy_port)

    def generate_ws_token(self, callback):
        """生成ws-Token"""
        data: dict = {
            "security": Security.SIGNED
        }

        self.add_request(
            method="GET",
            path="v1/u/ws/token",
            params={"time": int(time.time() * 1000)},
            callback=callback,
            data=data
        )

    def on_query_account(self, data: dict, request: Request) -> None:
        """资金查询回报"""
        if data.get('code') == 0:
            for balance in data["data"]:
                account: AccountData = AccountData(
                    accountid=balance['coin'],
                    balance=float(balance['balance']),
                    frozen=float(balance['freeze']),
                    gateway_name=self.gateway_name
                )

                if account.balance:
                    self.gateway.on_account(account)

            self.gateway.write_log("账户资金查询成功")

    def on_query_contract(self, data: dict, request: Request) -> None:
        """合约信息查询回报"""
        if data.get('code') == 0:
            for symbol in data['data']['pairs']:
                if symbol['state'] == 1:
                    base_currency: str = symbol['sellCoin']
                    quote_currency: str = symbol['buyCoin']
                    name: str = f"{base_currency.upper()}/{quote_currency.upper()}"

                    pricetick = symbol['minStepPrice']
                    min_volume = symbol['minQty']

                    contract: ContractData = ContractData(
                        symbol=symbol["symbol"],
                        exchange=Exchange.XEX,
                        name=name,
                        pricetick=pricetick,
                        size=1,
                        min_volume=min_volume,
                        product=Product.SPOT,
                        history_data=True,
                        gateway_name=self.gateway_name,
                        stop_supported=True
                    )
                    self.gateway.on_contract(contract)

                    symbol_contract_map[contract.symbol] = contract

            self.gateway.write_log("合约信息查询成功")
            self.query_order()

    def on_send_order(self, data: dict, request: Request) -> None:
        """委托下单回报"""
        pass

    def on_send_order_failed(self, status_code: str, request: Request) -> None:
        """委托下单失败服务器报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        msg: str = f"委托失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_send_order_error(
            self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """委托下单回报函数报错回报"""
        order: OrderData = request.extra
        order.status = Status.REJECTED
        self.gateway.on_order(order)

        if not issubclass(exception_type, (ConnectionError, SSLError)):
            self.on_error(exception_type, exception_value, tb, request)

    def on_cancel_order(self, data: dict, request: Request) -> None:
        """委托撤单回报"""
        pass

    def on_cancel_failed(self, status_code: str, request: Request) -> None:
        """撤单回报函数报错回报"""
        if request.extra:
            order = request.extra
            order.status = Status.REJECTED
            self.gateway.on_order(order)

        msg = f"撤单失败，状态码：{status_code}，信息：{request.response.text}"
        self.gateway.write_log(msg)

    def on_keep_user_stream(self, data: dict, request: Request) -> None:
        """延长listenKey有效期回报"""
        pass

    def on_keep_user_stream_error(
            self, exception_type: type, exception_value: Exception, tb, request: Request
    ) -> None:
        """延长listenKey有效期函数报错回报"""
        # 当延长listenKey有效期时，忽略超时报错
        if not issubclass(exception_type, TimeoutError):
            self.on_error(exception_type, exception_value, tb, request)


class XEXWebsocketClient(WebsocketClient):
    def unpack_data(self, data: str):
        """
        对字符串数据进行json格式解包

        如果需要使用json以外的解包格式，请重载实现本函数。
        """
        try:
            return json.loads(data)
        except json.JSONDecodeError:
            return data


class XEXSpotTradeWebsocketApi(XEXWebsocketClient):
    """XEX现货交易Websocket API"""

    def __init__(self, gateway: XEXSpotGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: XEXSpotGateway = gateway
        self.gateway_name = gateway.gateway_name

        self.heart_beat_future: asyncio.Future = None

    def connect(self, url: str, proxy_host: str, proxy_port: int) -> None:
        """连接Websocket交易频道"""
        self.init(url, proxy_host, proxy_port)
        self.start()
        # 心跳发送
        if self.heart_beat_future: self.heart_beat_future.cancel()
        self.heart_beat_future = run_coroutine_threadsafe(self.heart_beat(), self._loop)

    async def heart_beat(self):
        """40s发一次心跳"""
        while self._active:
            try:
                if self._ws:
                    asyncio.create_task(self._ws.send_str("ping"))
                    self.gateway.write_log("ping")
                    await asyncio.sleep(40)
                else:
                    await asyncio.sleep(1)
            except asyncio.CancelledError:
                raise
            except:
                pass

    def on_connected(self) -> None:
        """连接成功回报"""
        self.gateway.write_log("交易Websocket API连接成功")
        # 发送ws token
        self.gateway.rest_api.generate_ws_token(self.on_get_ws_token)

    def on_get_ws_token(self, data: dict, request: Request) -> None:
        """获取ws-Token回报"""
        ws_token = data['data']
        self.send_packet({"sub": "subUser", "token": ws_token})

    def on_packet(self, packet: Any) -> None:
        """推送数据回报"""
        if packet == 'succeed':
            self.gateway.write_log("订阅账户成功")
        elif packet == 'invalid_ws_token':
            self.gateway.write_log("ws token过期或者无效，重新请求获取ws token并发送给ws服务端")
            # 发送ws token
            self.gateway.rest_api.generate_ws_token(self.on_get_ws_token)
        elif packet == "pong":
            self.gateway.write_log("pong")
        elif isinstance(packet, dict) and packet.get("resType") == "uBalance":
            self.on_account(packet)
        elif isinstance(packet, dict) and packet.get("resType") == "uOrder":
            self.on_order(packet)

    def disconnect(self) -> None:
        """"主动断开webscoket链接"""
        self._active = False
        ws = self._ws
        if ws:
            coro = ws.close()
            run_coroutine_threadsafe(coro, self._loop)
        if self.heart_beat_future:
            self.heart_beat_future.cancel()

    def on_account(self, packet: dict) -> None:
        """资金更新推送"""
        # {"resType": "uBalance",
        #  "data": {
        #    "coin": "usdt",
        #     "freeze": "123", // 冻结
        #     "balance": "123", // 余额
        #     "availableBalance": "213"  // 可用
        # }}
        data = packet["data"]
        account: AccountData = AccountData(
            accountid=data["coin"],
            balance=float(data["balance"]),
            frozen=float(data["freeze"]),
            gateway_name=self.gateway_name
        )

        if account.balance:
            self.gateway.on_account(account)

    def on_order(self, packet: dict) -> None:
        """委托更新推送

        packet: {'data': {'avgPrice': '0',
                          'clientOrderId': '1685411493',
                          'createTime': 1685411494859,
                          'dealQty': '0',
                          'direction': 1,
                          'orderId': '233662556898590912',
                          'orderType': 1,
                          'origQty': '0.2',
                          'price': '1',
                          'state': 4,
                          'symbol': 'LTC_USDT'},
                'resType': 'uOrder'}
         """
        data = packet['data']
        # 过滤不支持类型的委托 1:LIMIT 2:MARKET
        if data['orderType'] not in (1, 2):
            return
        # 自定义单号
        orderid: str = data.get('clientOrderId')
        if orderid is None:
            return

        offset = self.gateway.get_order(orderid).offset if self.gateway.get_order(orderid) else None
        # vn order
        order: OrderData = OrderData(
            symbol=data["symbol"],
            exchange=Exchange.XEX,
            orderid=orderid,
            type=ORDERTYPE_INT_XEX2VT[data["orderType"]],
            direction=DIRECTION_INT_XEX2VT[data["direction"]],
            price=float(data["price"]),
            volume=float(data["origQty"]),
            traded=float(data["dealQty"]),
            status=STATUS_INT_XEX2VT[data["state"]],
            datetime=generate_datetime(data["createTime"]),
            gateway_name=self.gateway_name,
            offset=offset
        )
        setattr(order, "origin_orderId", data['orderId'])

        order_last_snapshot = self.gateway.get_order(orderid)

        self.gateway.on_order(order)
        # 计算trade
        if order.traded > order_last_snapshot.traded:
            trade_volume = order.traded - order_last_snapshot.traded
            contract: ContractData = symbol_contract_map.get(order.symbol, None)
            if contract:
                trade_volume = round_to(trade_volume, contract.min_volume)

            if not trade_volume:
                return

            trade: TradeData = TradeData(
                symbol=order.symbol,
                exchange=order.exchange,
                orderid=order.orderid,
                tradeid="-1",
                direction=order.direction,
                price=0,
                volume=trade_volume,
                datetime=generate_datetime(data["createTime"]),
                gateway_name=self.gateway_name,
                offset=offset
            )
            self.gateway.on_trade(trade)

    def on_disconnected(self) -> None:
        """连接断开回报"""
        self.gateway.write_log("交易Websocket API断开")
        self.gateway.rest_api.start_user_stream()


def generate_datetime(timestamp: float) -> datetime:
    """生成时间"""
    dt: datetime = datetime.fromtimestamp(timestamp / 1000)
    dt: datetime = CHINA_TZ.localize(dt)
    return dt

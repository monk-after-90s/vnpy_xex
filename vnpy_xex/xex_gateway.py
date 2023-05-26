import hashlib
import hmac
import urllib
from copy import copy
from datetime import datetime, timedelta
from enum import Enum
from threading import Lock

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
    SubscribeRequest
)
from vnpy.event import EventEngine
from vnpy_rest import RestClient, Request

# 中国时区
CHINA_TZ = pytz.timezone("Asia/Shanghai")

# 实盘REST API地址
# BASE_URL: str = "http://54.254.54.220:8069/"
BASE_URL: str = "https://openapi.hipiex.net/spot/"

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

# 委托类型映射
ORDERTYPE_VT2XEX: Dict[OrderType, str] = {
    OrderType.LIMIT: "LIMIT",
    OrderType.MARKET: "MARKET"
}
ORDERTYPE_XEX2VT: Dict[str, OrderType] = {v: k for k, v in ORDERTYPE_VT2XEX.items()}

# 买卖方向映射
DIRECTION_VT2XEX: Dict[Direction, str] = {
    Direction.LONG: "BUY",
    Direction.SHORT: "SELL"
}
DIRECTION_XEX2VT: Dict[str, Direction] = {v: k for k, v in DIRECTION_VT2XEX.items()}

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

        self.rest_api: "XEXSpotRestAPi" = XEXSpotRestAPi(self)

        self.orders: Dict[str, OrderData] = {}

    def connect(self, setting: dict):
        """连接交易接口"""
        key: str = setting["key"]
        secret: str = setting["secret"]
        proxy_host: str = setting["代理地址"]
        proxy_port: int = setting["代理端口"]

        self.rest_api.connect(key, secret, proxy_host, proxy_port)

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        return self.rest_api.send_order(req)

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

    def on_order(self, order: OrderData) -> None:
        """推送委托数据"""
        self.orders[order.orderid] = copy(order)
        super().on_order(order)

    def get_order(self, orderid: str) -> OrderData:
        """查询委托数据"""
        return self.orders.get(orderid, None)


class XEXSpotRestAPi(RestClient):
    """币安现货REST API"""

    def __init__(self, gateway: XEXSpotGateway) -> None:
        """构造函数"""
        super().__init__()

        self.gateway: XEXSpotGateway = gateway
        self.gateway_name: str = gateway.gateway_name

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
            query = urllib.parse.urlencode(sorted(request.params.items()))
            signature = hmac.new(self.secret, query.encode(), hashlib.sha256).hexdigest()

            # 添加请求头
            if request.headers is None: request.headers = {}
            headers = {
                "x_access_key": self.key,
                "x_signature": signature,
                'Content-Type': 'application/json',
            }
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
                self.gateway.on_order(order)
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

    def send_order(self, req: OrderRequest) -> str:
        """委托下单"""
        # 生成本地委托号
        orderid: str = str(self.connect_time + self._new_order_id())

        # 推送提交中事件
        order: OrderData = req.create_order_data(
            orderid,
            self.gateway_name
        )
        self.gateway.on_order(order)

        data: dict = {
            "security": Security.SIGNED
        }

        # 生成委托请求
        params: dict = {
            "symbol": req.symbol,
            "price": req.price,
            "amount": format(req.volume, ".5f"),
            "direction": DIRECTION_VT2XEX[req.direction],
            "orderType": ORDERTYPE_VT2XEX[req.type],
            "clientOrderId": orderid
        }

        self.add_request(
            method="POST",
            path="v1/trade/order/create",
            callback=self.on_send_order,
            data=data,
            params=params,
            extra=order,
            on_error=self.on_send_order_error,
            on_failed=self.on_send_order_failed
        )

        return order.vt_orderid

    def cancel_order(self, req: CancelRequest) -> None:
        """委托撤单"""
        data: dict = {
            "security": Security.SIGNED
        }

        params: dict = {
            "symbol": req.symbol.upper(),
            "origClientOrderId": req.orderid
        }

        order: OrderData = self.gateway.get_order(req.orderid)

        self.add_request(
            method="DELETE",
            path="/api/v3/order",
            callback=self.on_cancel_order,
            params=params,
            data=data,
            on_failed=self.on_cancel_failed,
            extra=order
        )

    def start_user_stream(self) -> Request:
        """用户数据推送"""
        ...

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


def generate_datetime(timestamp: float) -> datetime:
    """生成时间"""
    dt: datetime = datetime.fromtimestamp(timestamp / 1000)
    dt: datetime = CHINA_TZ.localize(dt)
    return dt

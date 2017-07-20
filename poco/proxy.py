# coding=utf-8
__author__ = 'lxn3032'


import copy
import numbers
import six
import time
from functools import wraps

from .rpc import RpcRemoteException, RpcTimeoutException
from .exceptions import PocoTargetTimeout, InvalidOperationException, PocoNoSuchNodeException, PocoTargetRemovedException
from .utils.retry import retries_when
from .utils.query_util import query_expr, build_query


def wait_for_appearance(func):
    @wraps(func)
    def wrapped(proxy, *args, **kwargs):
        try:
            return func(proxy, *args, **kwargs)
        except PocoNoSuchNodeException as e:
            try:
                proxy.wait_for_appearance(timeout=proxy.poco._pre_action_wait_for_appearance)
                return func(proxy, *args, **kwargs)
            except PocoTargetTimeout:
                raise e

    return wrapped


def refresh_when(error_type_str):
    def wrapper(func):
        @wraps(func)
        def wrapped(self, *args, **kwargs):
            try:
                return func(self, *args, **kwargs)
            except RpcRemoteException as e:
                if e.error_type == error_type_str:
                    # refresh node cache
                    self._do_query(multiple=False, refresh=True)
                    return func(self, *args, **kwargs)
                else:
                    raise
        return wrapped
    return wrapper


class UIObjectProxy(object):
    def __init__(self, poco, name=None, **attrs):
        self.query = build_query(name, **attrs)
        self.poco = poco

        self._evaluated = False
        self._query_multiple = False
        self._nodes = None  # 可能是远程node代理，也可能是远程[node]代理
        self._nodes_proxy_is_list = True
        self._sorted_childres = None  # 仅用于__getitem__时保存好已排序的child代理对象

        self._anchor = None  # 相对于包围盒的anchor定义，用于touch/swipe/drag操作的局部相对定位

    def child(self, name=None, **attrs):
        """
        以当前ui对象为基准，选择直系ui对象。可通过节点名和其余节点属性共同选择
        选择器规则同PocoUI.__call__

        :param name:
        :param attrs:
        :return: ui对象

        :raises:
            RpcRemoteException.NoSuchTargetException
            RpcRemoteException.NoSuchAttributeException
        """

        sub_query = build_query(name, **attrs)
        query = ('/', (self.query, sub_query))
        obj = UIObjectProxy(self.poco)
        obj.query = query
        return obj

    def children(self):
        """
        获取当前节点的所有孩子节点

        :return: 
        """

        return self.child()

    def offspring(self, name=None, **attrs):
        """
        以当前ui对象为基准，选择后代ui对象（所有后代）。可通过节点名和其余节点属性共同选择
        选择器规则同PocoUI.__call__

        :param name:
        :param attrs:
        :return: ui对象

        :raises:
            RpcRemoteException.NoSuchTargetException
            RpcRemoteException.NoSuchAttributeException
        """

        sub_query = build_query(name, **attrs)
        query = ('>', (self.query, sub_query))
        obj = UIObjectProxy(self.poco)
        obj.query = query
        return obj

    def sibling(self, name=None, **attrs):
        """
        以当前ui对象为基准，选择兄弟ui对象。可通过节点名和其余节点属性共同选择
        选择器规则同PocoUI.__call__

        :param name:
        :param attrs:
        :return: ui对象

        :raises:
            RpcRemoteException.NoSuchTargetException
            RpcRemoteException.NoSuchAttributeException
        """

        sub_query = build_query(name, **attrs)
        query = ('-', (self.query, sub_query))
        obj = UIObjectProxy(self.poco)
        obj.query = query
        return obj

    def __getitem__(self, item):
        """
        索引当前ui对象集合的第N个节点。在一个选择器的选择中可能会有多个满足条件的节点，例如物品栏的物品格子，使用数组索引可选出具体某一个。
        该函数默认按照空间排序（从左到右从上到下）后才进行选择
        
        警告：此方法有极大延迟，请勿频繁调用此方法。

        :param item: <int> 数组索引
        :return: ui对象

        :raise: RpcRemoteException.NoSuchTargetException
        :raise: PocoTargetRemovedException
        """

        nodes = self.nodes
        length = len(nodes)
        if not self._sorted_childres:
            self._sorted_childres = []
            for i in range(length):
                uiobj = UIObjectProxy(self.poco)
                uiobj.query = ('index', (self.query, i))
                uiobj._evaluated = True
                uiobj._query_multiple = True
                uiobj._nodes = self.poco.rpc.evaluate(nodes[i])
                uiobj._nodes_proxy_is_list = False
                pos = uiobj.get_position()
                self._sorted_childres.append((uiobj, pos))
        self._sorted_childres.sort(lambda a, b: cmp(list(reversed(a)), list(reversed(b))), key=lambda v: v[1])
        return self._sorted_childres[item][0]

    def __len__(self):
        """
        获取满足当前选择器的ui集合的节点个数

        :return: 当前ui集合的节点个数
        """

        if not self._nodes_proxy_is_list:
            return 1

        # 获取长度时总是multiple的
        if not self._query_multiple:
            nodes = self._do_query(multiple=True, refresh=True)
        else:
            nodes = self._nodes
        return len(nodes)

    def __iter__(self):
        """
        ui集合的节点迭代器，遍历所有满足选择条件的ui对象。
        遍历会默认按照从左到右从上到下的顺序，进行按顺序遍历。
        遍历过程中，还未遍历到的节点如果从画面中移除了则会抛出异常，已遍历的节点即使移除也不受影响。
        遍历顺序在遍历开始前已经确定，遍历过程中界面上的节点进行了重排则仍然按照之前的顺序进行遍历。

        :yield: ui对象
        
        :raise: PocoTargetRemovedException
        """

        # 节点数量太多时，就不按照控件顺序排序了
        nodes = self.nodes
        length = len(nodes)
        sorted_nodes = []
        for i in range(length):
            uiobj = UIObjectProxy(self.poco)
            uiobj.query = ('index', (self.query, i))
            uiobj._evaluated = True
            uiobj._query_multiple = True
            uiobj._nodes = self.poco.rpc.evaluate(nodes[i])
            uiobj._nodes_proxy_is_list = False
            pos = uiobj.get_position()
            sorted_nodes.append((uiobj, pos))
        sorted_nodes.sort(lambda a, b: cmp(list(reversed(a)), list(reversed(b))), key=lambda v: v[1])

        for obj, _ in sorted_nodes:
            yield obj

    @retries_when(RpcTimeoutException)
    @wait_for_appearance
    def click(self, anchor='anchor', sleep_interval=None):
        """
        点击当前ui对象，如果是ui对象集合则默认点击第一个

        :param anchor: 点击对象的局部坐标系，'anchor'表示对象本身的节点anchor，'center'表示对象包围盒中心点，
            其余anchor类型为list[2]/tuple[2]，以屏幕坐标系轴方向相同，对象包围盒左上角为[0, 0]，右下角为[1, 1]点。默认点击对象节点的anchor。
        :param sleep_interval: 点击后的静候时间，默认为poco的操作间隔
        :return: None
        
        :raise PocoNoSuchNodeException:
        """

        pos = self._position_of_anchor(anchor)
        self.poco.click(pos)
        if sleep_interval:
            time.sleep(sleep_interval)
        else:
            self.poco.wait_stable()

    @wait_for_appearance
    def swipe(self, dir, anchor='anchor', duration=0.5):
        """
        以当前对象的anchor为起点，swipe一段距离

        :param dir: 滑动方向，坐标系与屏幕坐标系相同。
        :param anchor: 点击对象的局部坐标系，'anchor'表示对象本身的节点anchor，'center'表示对象包围盒中心点，
            其余anchor类型为list[2]/tuple[2]，以屏幕坐标系轴方向相同，对象包围盒左上角为原点，右下角为[1, 1]点。默认点击对象节点的anchor。
        :param duration: 滑动持续时间
        :return: None
        
        :raise PocoNoSuchNodeException:
        """

        dir_vec = self._direction_vector_of(dir)
        origin = self._position_of_anchor(anchor)
        self.poco.swipe(origin, direction=dir_vec, duration=duration)

    def drag_to(self, target, duration=2):
        """
        以当前对象节点anchor为起点，拖动到目标对象节点anchor

        :param target: 目标对象/归一化坐标
        :param duration: 持续时间
        :return: None
        
        :raise PocoNoSuchNodeException:
        """

        if type(target) in (list, tuple):
            target_pos = target
        else:
            target_pos = target._position_of_anchor('anchor')
        origin_pos = self._position_of_anchor('anchor')
        dir = [target_pos[0] - origin_pos[0], target_pos[1] - origin_pos[1]]
        self.swipe(dir, duration=duration)

    def anchor(self, a):
        """
        设置对象的操作定位点，相对于对象包围盒。Immutable操作，返回一个新的对象代理，原对象不受影响

        :param a: anchor，anchor/center，或其余list[2]
        :return: 新的对象代理
        """

        ret = copy.copy(self)
        ret._anchor = a
        return ret

    def _position_of_anchor(self, anchor):
        anchor = self._anchor or anchor
        screen_resolution = self.poco.screen_resolution
        if anchor == 'anchor':
            pos = self.attr('anchorPosition')
            pos = [pos[0] / screen_resolution[0], pos[1] / screen_resolution[1]]
        elif anchor == 'center':
            pos = self.attr('screenPosition')
            pos = [pos[0] / screen_resolution[0], pos[1] / screen_resolution[1]]
        elif type(anchor) in (list, tuple):
            center = self.get_position()
            size = self.get_size()
            pos = [(anchor[0] - 0.5) * size[0] + center[0], (anchor[1] - 0.5) * size[1] + center[1]]
        else:
            raise TypeError('Unsupported anchor type {}. '
                            'Only "anchor/center" or 2 elements list/tuple available.'.format(type(anchor)))
        return pos

    def _direction_vector_of(self, dir):
        if dir == 'up':
            dir_vec = [0, -0.1]
        elif dir == 'down':
            dir_vec = [0, 0.1]
        elif dir == 'left':
            dir_vec = [-0.1, 0]
        elif dir == 'right':
            dir_vec = [0.1, 0]
        elif type(dir) in (list, tuple):
            dir_vec = dir
        else:
            raise TypeError('Unsupported direction type {}. '
                            'Only "up/down/left/right" or 2 elements list/tuple available.'.format(type(dir)))
        return dir_vec

    def wait_for_appearance(self, timeout=120):
        """
        等待当前ui对象出现

        :param timeout: 最长等待时间
        :return: None

        :raise PocoTargetTimeout: 当超时时抛出该异常
        """
        start = time.time()
        while not self.exists():
            self.poco.sleep_for_polling_interval()
            if time.time() - start > timeout:
                raise PocoTargetTimeout('appearance', self.query)

    def wait_for_disappearance(self, timeout=120):
        """
        等待当前ui对象消失

        :param timeout: 最长等待时间
        :return: None

        :raise PocoTargetTimeout: 当超时时抛出该异常
        """
        start = time.time()
        while self.exists():
            self.poco.sleep_for_polling_interval()
            if time.time() - start > timeout:
                raise PocoTargetTimeout('disappearance', self.query)

    @retries_when(RpcTimeoutException)
    @refresh_when("NodeHasBeenRemovedException")
    def attr(self, name):
        """
        获取当前ui对象属性，如果为ui集合时，默认只取第一个ui对象的属性。
        坐标、向量、尺寸均为屏幕坐标系的下的值，并非归一化值，字符串均为utf-8编码

        :param name: 属性名，只可能是下列之一
            visible: <bool>是否可见
            text: <str(utf-8)/NoneType> 节点文本值
            type: <str> 节点类型
            enable: <bool> 节点正常可用，通常是对于可操作的控件类
            touchable: <bool> 是否可点击，目前没什么用
            screenPosition: <list[2]> 节点包围盒中心点在屏幕上的坐标
            anchorPosition: <list[2]> 节点anchor点在屏幕上的坐标
            size: <list[2]> 节点换算到屏幕上的尺寸，[width, height]
            name: <str> 节点名称
            direction_vector: <list[2]> 节点极轴在在屏幕坐标上的向量，单位向量
        :return: 以上属性值为空时返回None，否则返回对应属性值

        :raise PocoNoSuchNodeException: 当查询节点不存在是
        :raise RpcRemoteException.NoSuchAttributeException: 当查询不是以上的属性名时抛出该异常
        
        :note: 自动捕获RpcRemoteException.NodeHasBeenRemovedException
               远程节点对象可能已经从渲染树中移除，这样需要重新选择这个节点了
        """

        # 优化速度，只选择第一个匹配到的节点
        nodes = self._do_query(multiple=False)
        val = self.poco.rpc.getattr(nodes, name)
        return val

    @retries_when(RpcTimeoutException)
    @refresh_when("NodeHasBeenRemovedException")
    def setattr(self, name, val):
        nodes = self._do_query(multiple=False)
        self.poco.rpc.setattr(nodes, name, val)

    def exists(self):
        """
        判断节点是否存在visible节点树中。只要在节点树中的可见节点均为exists，包括屏幕外的和被遮挡的

        :return: 节点是否存在， True/False
        """

        try:
            return self.attr('visible')
        except (RpcRemoteException, PocoNoSuchNodeException):
            return False

    def visible(self):
        """
        判断节点是否可见。TODO：功能还没确定，不要用这个方法，后面可能还会加上判断是否在屏幕外等

        :return: True/False
        """

        return self.attr('visible')

    def enabled(self):
        """
        判断节点是否使能、可用

        :return: True/False
        """

        return self.attr('enable')

    def touchable(self):
        """
        判断节点是否可点击，不是所有类型节点都有这个属性

        :return: True/False

        :raise RpcRemoteException.NoSuchAttributeException: 当查询不是以上的属性名时抛出该异常
        """

        return self.attr('touchable')

    def get_text(self):
        """
        获取节点上的文本值，utf-8编码

        :return: 节点上的文本值，以utf-8编码
        """

        text = self.attr('text')
        if six.PY2 and type(text) is unicode:
            text = text.encode('utf-8')
        return text

    def set_text(self, text):
        """
        给TextField节点设置text值

        :param text: 要设置的text值
        :return: None

        :raise InvalidOperationException: 在一个不可设置文本值的节点上设置节点时会抛出该异常
        """

        try:
            self.setattr('text', text)
        except RpcRemoteException as e:
            raise InvalidOperationException('"{}" of "{}"'.format(e.message, self))

    def get_name(self):
        """
        获取节点名

        :return: 节点名
        """

        return self.attr('name')

    def get_size(self):
        """
        获取节点在屏幕上的归一化尺寸

        :return: 格式为[width, height]的list, width,height ∈ [0, 1]
        """

        size_in_screen = self.attr('size')
        screen_resolution = self.poco.screen_resolution
        return [size_in_screen[0] / screen_resolution[0], size_in_screen[1] / screen_resolution[1]]

    def get_position(self):
        position_in_screen = self.attr('screenPosition')
        screen_resolution = self.poco.screen_resolution
        return [position_in_screen[0] / screen_resolution[0], position_in_screen[1] / screen_resolution[1]]

    def get_bounds(self):
        size = self.get_size()
        top_left = self._position_of_anchor([0, 0])

        # t, r, b, l
        bounds = [top_left[1], top_left[0] + size[0], top_left[1] + size[1], top_left[0]]
        return bounds

    def __str__(self):
        return u'UIObjectProxy of "{}"'.format(query_expr(self.query))

    __repr__ = __str__

    @property
    def nodes(self):
        """
        访问所选择对象的远程节点对象

        :return: RpcRemoteObjectProxy. Rpc远程对象代理
        """
        return self._do_query()

    def _do_query(self, multiple=True, refresh=False):
        if not self._evaluated or refresh:
            self._nodes = self.poco.rpc.select(self.query, multiple)
            if len(self._nodes) == 0:
                raise PocoNoSuchNodeException(self)
            self._evaluated = True
            self._query_multiple = multiple
        return self._nodes

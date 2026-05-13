class GiftCodeError(Exception):
    pass


class GiftCodeFormatError(GiftCodeError):
    """
    结构性错误：
    不是礼品码 / 格式损坏
    """
    pass


class GiftCodeRedeemError(GiftCodeError):
    """
    业务错误：
    已使用 / 用户不匹配 / 过期
    """
    pass
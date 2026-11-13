"""Installed as WCFI_Bridge in Live's User Library; no wcfi dependencies."""


def create_instance(c_instance):
    from .surface import WCFIBridge

    return WCFIBridge(c_instance)

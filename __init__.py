def classFactory(iface):
    from .merge_split import MergeAndSplit
    return MergeAndSplit(iface)

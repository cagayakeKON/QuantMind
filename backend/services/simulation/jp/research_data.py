"""Japan-qualified inputs for the existing TradingAgents tool router."""


def route_tool(method, *args, **kwargs):
    from tradingagents.dataflows.quantmind_local import route_registered_market

    return route_registered_market(method, *args, **kwargs)

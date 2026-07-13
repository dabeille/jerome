"""Step 0.1.4: verify keys + connectivity end to end. PAPER MODE ONLY.

    python -m scripts.smoke_test
"""

import sys

from bot import broker, config, data


def main() -> None:
    config.validate()
    if config.MODE != "paper":
        sys.exit("Smoke test only runs in paper mode (BOT_MODE=paper).")

    acct = broker.account()
    print(f"1/3 Auth OK — paper equity ${float(acct.equity):,.2f}")

    bars = data.get_daily_bars(["SPY"], years=1, refresh=True)
    last = bars["SPY"].iloc[-1]
    print(f"2/3 Data OK — SPY last close {last['close']:.2f}")

    from alpaca.trading.requests import LimitOrderRequest
    from alpaca.trading.enums import OrderSide, TimeInForce
    order = broker.client().submit_order(LimitOrderRequest(
        symbol="SPY", qty=1, side=OrderSide.BUY,
        time_in_force=TimeInForce.DAY,
        limit_price=round(float(last["close"]) * 0.5, 2),  # far from market; won't fill
    ))
    broker.client().cancel_order_by_id(order.id)
    print(f"3/3 Order round-trip OK — placed + cancelled {order.id}")
    print("\nSmoke test passed.")


if __name__ == "__main__":
    main()

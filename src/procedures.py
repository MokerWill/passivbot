import glob
import json
import os
import traceback
import asyncio
from datetime import datetime, timezone
from time import time
import numpy as np
import pprint
from copy import deepcopy
import argparse
from collections import defaultdict
from collections.abc import Sized
import sys
from typing import Union, Optional, Set, Any
from pathlib import Path

try:
    import hjson
except:
    print("hjson not found, trying without...")
    pass
try:
    import pandas as pd
except:
    print("pandas not found, trying without...")
    pass

from pure_funcs import (
    numpyize,
    candidate_to_live_config,
    ts_to_date,
    ts_to_date_utc,
    get_dummy_settings,
    config_pretty_str,
    date_to_ts2,
    get_template_live_config,
    sort_dict_keys,
    make_compatible,
    determine_passivbot_mode,
    date2ts_utc,
    remove_OD,
    symbol_to_coin,
    str2bool,
    flatten,
)


def format_config(config: dict, verbose=True, live_only=False) -> dict:
    # attempts to format a config to v7 config
    template = get_template_live_config("v7")
    cmap = {
        "ddown_factor": "entry_grid_double_down_factor",
        "initial_eprice_ema_dist": "entry_initial_ema_dist",
        "initial_qty_pct": "entry_initial_qty_pct",
        "markup_range": "close_grid_markup_range",
        "min_markup": "close_grid_min_markup",
        "rentry_pprice_dist": "entry_grid_spacing_pct",
        "rentry_pprice_dist_wallet_exposure_weighting": "entry_grid_spacing_weight",
        "ema_span_0": "ema_span_0",
        "ema_span_1": "ema_span_1",
    }
    cmap_inv = {v: k for k, v in cmap.items()}
    if all(
        [
            x in config
            for x in [
                "user",
                "pnls_max_lookback_days",
                "loss_allowance_pct",
                "stuck_threshold",
                "unstuck_close_pct",
                "TWE_long",
                "TWE_short",
                "universal_live_config",
            ]
        ]
    ):
        # PB multi live config
        for key1 in template["live"]:
            if key1 in config:
                template["live"][key1] = config[key1]
        if config["approved_symbols"] and isinstance(config["approved_symbols"], dict):
            template["live"]["coin_flags"] = config["approved_symbols"]
        template["live"]["approved_coins"] = sorted(set(config["approved_symbols"]))
        template["live"]["ignored_coins"] = sorted(set(config["ignored_symbols"]))
        for pside in ["long", "short"]:
            for key in template["bot"][pside]:
                if key in cmap_inv and cmap_inv[key] in config["universal_live_config"][pside]:
                    template["bot"][pside][key] = config["universal_live_config"][pside][
                        cmap_inv[key]
                    ]
            close_grid_qty_pct = 1.0 / round(config["universal_live_config"][pside]["n_close_orders"])
            template["bot"][pside]["close_grid_qty_pct"] = 1.0 / round(
                config["universal_live_config"][pside]["n_close_orders"]
            )
            for key in [
                "close_trailing_grid_ratio",
                "close_trailing_retracement_pct",
                "close_trailing_threshold_pct",
                "entry_trailing_grid_ratio",
                "entry_trailing_retracement_pct",
                "entry_trailing_threshold_pct",
                "unstuck_ema_dist",
            ]:
                template["bot"][pside][key] = 0.0
            if config[f"n_longs"] == 0 and config[f"n_shorts"] == 0:
                forager_mode = False
                # not forager mode
                n_positions = len(template["live"]["coin_flags"])
            else:
                n_positions = config[f"n_{pside}s"]
            template["bot"][pside]["n_positions"] = n_positions
            template["bot"][pside]["unstuck_close_pct"] = config["unstuck_close_pct"]
            template["bot"][pside]["unstuck_loss_allowance_pct"] = config["loss_allowance_pct"]
            template["bot"][pside]["unstuck_threshold"] = config["stuck_threshold"]
            template["bot"][pside]["total_wallet_exposure_limit"] = (
                config[f"TWE_{pside}"] if config[f"{pside}_enabled"] else 0.0
            )
        result = template
    elif "common" in config:
        # older v7 config type
        for k0 in ["backtest", "live", "optimize", "bot"]:
            for k1 in config[k0]:
                if k1 in template[k0]:
                    template[k0][k1] = config[k0][k1]
        for key in config["common"]:
            if key in template["live"]:
                template["live"][key] = config["common"][key]
        template["live"]["approved_coins"] = config["common"]["approved_symbols"]
        template["live"]["coin_flags"] = config["common"]["symbol_flags"]
        result = template
    elif all([k in config for k in template]):
        result = deepcopy(config)
    elif "config" in config and all([k in config["config"] for k in template]):
        result = deepcopy(config["config"])
    elif "bot" in config and "live" in config:
        # live only config
        result = deepcopy(config)
        for key in ["optimize", "backtest"]:
            if key not in result:
                result[key] = deepcopy(template[key])
    else:
        raise Exception(f"failed to format config")
    for k0, v0, v1 in [
        ("close_trailing_qty_pct", 1.0, [0.05, 1.0]),
        (
            "filter_rolling_window",
            (
                result["live"]["ohlcv_rolling_window"]
                if "ohlcv_rolling_window" in result["live"]
                else 60.0
            ),
            [10.0, 1440.0],
        ),
        (
            "filter_relative_volume_clip_pct",
            (
                result["live"]["relative_volume_filter_clip_pct"]
                if "relative_volume_filter_clip_pct" in result["live"]
                else 0.5
            ),
            [0.0, 1.0],
        ),
    ]:
        for pside in ["long", "short"]:
            if k0 not in result["bot"][pside]:
                result["bot"][pside][k0] = v0
                if verbose:
                    print(f"adding missing backtest parameter {pside} {k0}: {v0}")
            opt_key = f"{pside}_{k0}"
            if opt_key not in result["optimize"]["bounds"]:
                result["optimize"]["bounds"][opt_key] = v1
                if verbose:
                    print(f"adding missing optimize parameter {pside} {opt_key}: {v1}")
    for k0, src, dst in [
        ("live", "minimum_market_age_days", "minimum_coin_age_days"),
        ("live", "noisiness_rolling_mean_window_size", "ohlcv_rolling_window"),
    ]:
        if src in result[k0]:
            result[k0][dst] = deepcopy(result[k0][src])
            if verbose:
                print(f"renaming parameter {k0} {src}: {dst}")
            del result[k0][src]
    if "exchange" in result["backtest"] and isinstance(result["backtest"]["exchange"], str):
        result["backtest"]["exchanges"] = [result["backtest"]["exchange"]]
        if verbose:
            print(
                f"changed backtest.exchange: {result['backtest']['exchange']} -> backtest.exchanges: [{result['backtest']['exchange']}]"
            )
        del result["backtest"]["exchange"]
    for k0 in template:
        for k1 in template[k0]:
            if k0 not in result:
                raise Exception(f"Fatal: {k0} missing from config")
            if k1 not in result[k0]:
                result[k0][k1] = template[k0][k1]
                if verbose:
                    print(f"adding missing parameter {k0}.{k1}: {template[k0][k1]}")
    if not live_only:
        for k_coins in ["approved_coins", "ignored_coins"]:
            path = result["live"][k_coins]
            if isinstance(path, list):
                if len(path) == 1 and isinstance(path[0], str) and os.path.exists(path[0]):
                    if any([path[0].endswith(k) for k in [".txt", ".json", ".hjson"]]):
                        path = path[0]
            if isinstance(path, str):
                if os.path.exists(path):
                    try:
                        content = read_external_coins_lists(path)
                        if content:
                            if verbose:
                                if result["live"][k_coins] != content:
                                    print(f"set {k_coins} {content}")
                            result["live"][k_coins] = content
                    except Exception as e:
                        print(f"failed to load {k_coins} from file {path} {e}")
                else:
                    print(f"path to {k_coins} file does not exist {path}")
                    result["live"][k_coins] = {"long": [], "short": []}
            if isinstance(result["live"][k_coins], list):
                result["live"][k_coins] = {
                    "long": deepcopy(result["live"][k_coins]),
                    "short": deepcopy(result["live"][k_coins]),
                }
        result["backtest"]["symbols"] = {}
        for exchange in result["backtest"]["exchanges"]:
            eligible_symbols = get_all_eligible_symbols(exchange)
            ignored_coins = coins_to_symbols(
                set(flatten(result["live"]["ignored_coins"].values())),
                eligible_symbols=eligible_symbols,
                exchange=exchange,
                verbose=verbose,
            )
            approved_coins = coins_to_symbols(
                set(flatten(result["live"]["approved_coins"].values())),
                eligible_symbols=eligible_symbols,
                exchange=exchange,
                verbose=verbose,
            )
            if approved_coins:
                result["backtest"]["symbols"][exchange] = [
                    x
                    for x in coins_to_symbols(
                        sorted(approved_coins),
                        eligible_symbols=eligible_symbols,
                        exchange=exchange,
                        verbose=verbose,
                    )
                    if x not in ignored_coins
                ]
            else:
                result["backtest"]["symbols"][exchange] = [
                    s for s in sorted(get_all_eligible_symbols(exchange)) if s not in ignored_coins
                ]
    result["backtest"]["end_date"] = format_end_date(result["backtest"]["end_date"])
    return result


def get_all_eligible_symbols(exchange="binance"):
    exchange_map = {
        "bybit": "bybit",
        "binance": "binanceusdm",
        # "bitget": "bitget", TODO
        # "hyperliquid": "hyperliquid", TODO
        # "gateio": "gateio", TODO
    }
    quote_map = {k: "USDT" for k in exchange_map}
    quote_map["hyperliquid"] = "USDC"
    if exchange not in exchange_map:
        raise Exception(f"only exchanges {list(exchange_map.values())} are supported for backtesting")
    filepath = make_get_filepath(f"caches/{exchange}/eligible_symbols.json")
    loaded_json = None
    try:
        loaded_json = json.load(open(filepath))
        if utc_ms() - get_file_mod_utc(filepath) > 1000 * 60 * 60 * 24:
            print(f"Eligible_symbols cache more than 24h old. Fetching new.")
        else:
            return loaded_json
    except Exception as e:
        print(f"failed to load {filepath}. Fetching from {exchange}")
        pass
    try:
        quote = quote_map[exchange]
        import ccxt

        cc = getattr(ccxt, exchange_map[exchange])()
        markets = cc.fetch_markets()
        symbols = [
            x["symbol"] for x in markets if "symbol" in x and x["symbol"].endswith(f":{quote}")
        ]
        eligible_symbols = sorted(set([x.replace(f"/{quote}:", "") for x in symbols]))
        eligible_symbols = [x for x in eligible_symbols if x]
        json.dump(eligible_symbols, open(filepath, "w"))
        return eligible_symbols
    except Exception as e:
        print(f"error fetching eligible symbols {e}")
        if loaded_json:
            print(f"using cached data")
            return loaded_json
        raise Exception("unable to fetch or load from cache")


def coin_to_symbol(coin, eligible_symbols=None, quote="USDT", verbose=True):
    if eligible_symbols is None:
        eligible_symbols = get_all_eligible_symbols()
    # first check if there is a single match
    candidates = {s for s in eligible_symbols if coin in s}
    if len(candidates) == 1:
        return next(iter(candidates))

    # next check if coin/quote:quote has a match
    candidate_symbol = f"{coin}/{quote}:{quote}"
    if candidate_symbol in eligible_symbols:
        return candidate_symbol

    # next format coin (e.g. 1000SHIB -> SHIB, kPEPE -> PEPE, etc)
    coinf = symbol_to_coin(coin)
    candidates = {s for s in eligible_symbols if coinf in s}
    if len(candidates) == 1:
        return next(iter(candidates))
    # next check if multiple matches
    if len(candidates) > 1:
        for candidate in candidates:
            candidate_coin = symbol_to_coin(candidate)
            if candidate_coin == coinf:
                return candidate
        if verbose:
            print(f"coin_to_symbol {coin} {coinf}: ambiguous coin, multiple candidates {candidates}")
    else:
        if verbose:
            print(f"coin_to_symbol no candidate symbol for {coin}, {coinf}")
    return ""


def coins_to_symbols(coins: [str], eligible_symbols=None, exchange=None, verbose=True):
    if eligible_symbols is None:
        eligible_symbols = get_all_eligible_symbols(exchange)
    symbols = [coin_to_symbol(x, eligible_symbols=eligible_symbols, verbose=verbose) for x in coins]
    return sorted(set([x for x in symbols if x]))


def format_end_date(end_date) -> str:
    if end_date in ["today", "now", "", None]:
        ms2day = 1000 * 60 * 60 * 24
        end_date = ts_to_date_utc((utc_ms() - ms2day) // ms2day * ms2day)
    else:
        end_date = ts_to_date_utc(date_to_ts2(end_date))
    return end_date[:10]


def load_config(filepath: str, live_only=False, verbose=True) -> dict:
    # loads hjson or json v7 config
    try:
        config = load_hjson_config(filepath)
        config = format_config(config, live_only=live_only, verbose=verbose)
        return config
    except Exception as e:
        traceback.print_exc()
        raise Exception(f"failed to load config {filepath}: {e}")


def dump_config(config: dict, filepath: str):
    dump_pretty_json(config, filepath)


def dump_pretty_json(data: dict, filepath: str):
    try:
        with open(filepath, "w") as f:
            f.write(config_pretty_str(sort_dict_keys(data)))
    except Exception as e:
        raise Exception(f"failed to dump data {filepath}: {e}")


def load_live_config(live_config_path: str) -> dict:
    try:
        live_config = json.load(open(live_config_path))
        return sort_dict_keys(numpyize(make_compatible(live_config)))
    except Exception as e:
        raise Exception(f"failed to load live config {live_config_path} {e}")


def dump_live_config(config: dict, path: str):
    pretty_str = config_pretty_str(candidate_to_live_config(config))
    with open(path, "w") as f:
        f.write(pretty_str)


def load_config_files(config_paths: []) -> dict:
    config = {}
    for config_path in config_paths:
        try:
            loaded_config = hjson.load(open(config_path, encoding="utf-8"))
            config = {**config, **loaded_config}
        except Exception as e:
            raise Exception("failed to load config file", config_path, e)
    return config


def load_hjson_config(config_path: str) -> dict:
    try:
        return remove_OD(hjson.load(open(config_path, encoding="utf-8")))
    except Exception as e:
        raise Exception(f"failed to load config file {config_path} {e}")


def prepare_backtest_config(args) -> dict:
    """
    takes argparse args, returns dict with backtest config
    """
    config = load_hjson_config(args.backtest_config_path)

    if args.symbols is not None:
        config["symbols"] = args.symbols.split(",")
    for key in [
        "user",
        "start_date",
        "end_date",
        "starting_balance",
        "market_type",
        "base_dir",
    ]:
        if hasattr(args, key) and getattr(args, key) is not None:
            config[key] = getattr(args, key)
        elif key not in config:
            config[key] = None
    if args.market_type is None:
        config["spot"] = False
    else:
        config["spot"] = args.market_type == "spot"
    config["start_date"] = ts_to_date_utc(date_to_ts2(config["start_date"]))[:10]
    config["end_date"] = format_end_date(config["end_date"])
    config["exchange"] = load_exchange_key_secret_passphrase(config["user"])[0]
    config["session_name"] = (
        f"{config['start_date'].replace(' ', '').replace(':', '').replace('.', '')}_"
        f"{config['end_date'].replace(' ', '').replace(':', '').replace('.', '')}"
    )
    if config["exchange"] in ["okx", "kucoin"]:
        config["ohlcv"] = True
    elif hasattr(args, "ohlcv"):
        if args.ohlcv is None:
            if "ohlcv" not in config:
                config["ohlcv"] = True
        else:
            if args.ohlcv.lower() in ["y", "t", "yes", "true"]:
                config["ohlcv"] = True
            else:
                config["ohlcv"] = False
    elif "ohlcv" not in config:
        config["ohlcv"] = True

    if config["base_dir"].startswith("~"):
        raise Exception("error: using the ~ to indicate the user's home directory is not supported")
    if len(config["symbols"]) == 1:
        config["symbol"] = config["symbols"][0]
        base_dirpath = os.path.join(
            config["base_dir"],
            f"{config['exchange']}{'_spot' if 'spot' in config['market_type'] else ''}",
            config["symbol"],
        )
        config["caches_dirpath"] = make_get_filepath(os.path.join(base_dirpath, "caches", ""))
        config["plots_dirpath"] = make_get_filepath(os.path.join(base_dirpath, "plots", ""))
        add_market_specific_settings(config)
    return config


def prepare_optimize_config(args) -> dict:
    config = prepare_backtest_config(args)
    config.update(load_hjson_config(args.optimize_config_path))

    for key in ["starting_configs", "iters", "algorithm", "clip_threshold", "passivbot_mode"]:
        if hasattr(args, key) and getattr(args, key) is not None:
            config[key] = getattr(args, key)
        elif key not in config:
            config[key] = None

    algo_map = {
        "h": "harmony_search",
        "hs": "harmony_search",
        "harmony_search": "harmony_search",
        "harmony-search": "harmony_search",
        "p": "particle_swarm_optimization",
        "pso": "particle_swarm_optimization",
        "PSO": "particle_swarm_optimization",
        "particle_swarm_optimization": "particle_swarm_optimization",
        "particle-swarm-optimization": "particle_swarm_optimization",
    }
    assert config["algorithm"] in algo_map, f"unknown algorithm {config['algorithm']}"
    config["algorithm"] = algo_map[config["algorithm"]]

    pm_map = {
        "r": "recursive_grid",
        "recursive": "recursive_grid",
        "recursive_grid": "recursive_grid",
        "recursive-grid": "recursive_grid",
        "n": "neat_grid",
        "neat": "neat_grid",
        "neat_grid": "neat_grid",
        "neat-grid": "neat_grid",
        "c": "clock",
        "clock": "clock",
    }
    assert config["passivbot_mode"] in pm_map, f"unknown passivbot mode {config['passivbot_mode']}"
    config["passivbot_mode"] = pm_map[config["passivbot_mode"]]

    if args.optimize_output_path is None:
        output_base_dir = f"results_{config['algorithm']}_{config['passivbot_mode']}/"
    else:
        output_base_dir = args.optimize_output_path
    identifying_name = (
        f"{len(config['symbols'])}_symbols" if len(config["symbols"]) > 1 else config["symbols"][0]
    )
    now_date = ts_to_date(time())[:19].replace(":", "-")
    config["results_fpath"] = os.path.join(output_base_dir, f"{now_date}_{identifying_name}", "")

    for key in [
        "skip_multicoin",
        "skip_singlecoin",
        "skip_non_matching_single_coin",
        "skip_matching_single_coin",
    ]:
        if getattr(args, key) is not None:
            if getattr(args, key).lower() in ["y", "t", "yes", "true"]:
                config["starting_configs_filtering_conditions"].append(key)
            elif key in config["starting_configs_filtering_conditions"]:
                config["starting_configs_filtering_conditions"] = [
                    x for x in config["starting_configs_filtering_conditions"] if x != key
                ]

    return config


def add_market_specific_settings(config):
    mss = config["caches_dirpath"] + "market_specific_settings.json"
    symbol = config["symbol"]
    try:
        print(f"fetching market_specific_settings for {symbol}...")
        market_specific_settings = fetch_market_specific_settings(config)
        json.dump(market_specific_settings, open(mss, "w"), indent=4)
    except Exception as e:
        traceback.print_exc()
        print(f"\nfailed to fetch market_specific_settings for symbol {symbol}", e, "\n")
        try:
            if os.path.exists(mss):
                market_specific_settings = json.load(open(mss))
                print("using cached market_specific_settings")
            else:
                raise Exception(f"no cached market_specific_settings for symbol {symbol}")
        except:
            raise Exception(f"failed to load cached market_specific_settings for symbol {symbol}")
    config.update(market_specific_settings)


def ensure_parent_directory(
    filepath: Union[str, Path], mode: int = 0o755, exist_ok: bool = True
) -> Path:
    """
    Creates directory and subdirectories for a given filepath if they don't exist,
    then returns the path as a Path object.

    Args:
        filepath: String or Path object representing the file or directory path
        mode: Directory permissions (default: 0o755)
        exist_ok: If False, raise FileExistsError if directory exists (default: True)

    Returns:
        Path object representing the input filepath

    Raises:
        TypeError: If filepath is neither str nor Path
        PermissionError: If user lacks permission to create directory
        FileExistsError: If directory exists and exist_ok is False
    """
    try:
        # Convert to Path object
        path = Path(filepath)

        # Determine if the path points to a directory
        # (either ends with separator or is explicitly a directory)
        if str(path).endswith(os.path.sep) or (path.exists() and path.is_dir()):
            dirpath = path
        else:
            dirpath = path.parent

        # Create directory if it doesn't exist
        if not dirpath.exists():
            dirpath.mkdir(parents=True, mode=mode, exist_ok=exist_ok)
        elif not exist_ok:
            raise FileExistsError(f"Directory already exists: {dirpath}")

        return path

    except TypeError as e:
        raise TypeError(f"filepath must be str or Path, not {type(filepath)}") from e
    except PermissionError as e:
        raise PermissionError(f"Permission denied creating directory: {dirpath}") from e
    except Exception as e:
        raise RuntimeError(f"Error processing filepath: {str(e)}") from e


def make_get_filepath(filepath: str) -> str:
    """
    if not is path, creates dir and subdirs for path, returns path
    """
    dirpath = os.path.dirname(filepath) if filepath[-1] != "/" else filepath
    if not os.path.isdir(dirpath):
        os.makedirs(dirpath)
    return filepath


def load_user_info(user: str, api_keys_path="api-keys.json") -> dict:
    if api_keys_path is None:
        api_keys_path = "api-keys.json"
    try:
        api_keys = json.load(open(api_keys_path))
    except Exception as e:
        raise Exception(f"error loading api keys file {api_keys_path} {e}")
    if user not in api_keys:
        raise Exception(f"user {user} not found in {api_keys_path}")
    return {
        k: api_keys[user][k] if k in api_keys[user] else ""
        for k in [
            "exchange",
            "key",
            "secret",
            "passphrase",
            "wallet_address",
            "private_key",
            "is_vault",
        ]
    }


def load_exchange_key_secret_passphrase(
    user: str, api_keys_path="api-keys.json"
) -> (str, str, str, str):
    if api_keys_path is None:
        api_keys_path = "api-keys.json"
    try:
        keyfile = json.load(open(api_keys_path))
        if user in keyfile:
            return (
                keyfile[user]["exchange"],
                keyfile[user]["key"],
                keyfile[user]["secret"],
                keyfile[user]["passphrase"] if "passphrase" in keyfile[user] else "",
            )
        else:
            print("Looks like the keys aren't configured yet, or you entered the wrong username!")
        raise Exception("API KeyFile Missing!")
    except FileNotFoundError:
        print("File Not Found!")
        raise Exception("API KeyFile Missing!")


def load_broker_code(exchange: str) -> str:
    try:
        return hjson.load(open("broker_codes.hjson"))[exchange]
    except Exception as e:
        print(f"failed to load broker code", e)
        traceback.print_exc()
        return ""


def print_(args, r=False, n=False):
    line = ts_to_date(utc_ms())[:19] + "  "
    # line = ts_to_date(local_time())[:19] + '  '
    str_args = "{} " * len(args)
    line += str_args.format(*args)
    if n:
        print("\n" + line, end=" ")
    elif r:
        print("\r" + line, end=" ")
    else:
        print(line)
    return line


async def fetch_market_specific_settings_old(config: dict):
    user = config["user"]
    exchange = config["exchange"]
    symbol = config["symbol"]
    tmp_live_settings = get_dummy_settings(config)
    settings_from_exchange = {}
    if exchange == "binance":
        if "spot" in config["market_type"]:
            bot = await create_binance_bot_spot(tmp_live_settings)
            settings_from_exchange["maker_fee"] = 0.001
            settings_from_exchange["taker_fee"] = 0.001
            settings_from_exchange["spot"] = True
            settings_from_exchange["hedge_mode"] = False
        else:
            bot = await create_binance_bot(tmp_live_settings)
            settings_from_exchange["maker_fee"] = 0.0002
            settings_from_exchange["taker_fee"] = 0.0004
            settings_from_exchange["spot"] = False
        settings_from_exchange["exchange"] = "binance"
    elif exchange == "binance_us":
        bot = await create_binance_bot_spot(tmp_live_settings)
        settings_from_exchange["maker_fee"] = 0.001
        settings_from_exchange["taker_fee"] = 0.001
        settings_from_exchange["spot"] = True
        settings_from_exchange["hedge_mode"] = False
        settings_from_exchange["exchange"] = "binance"

    elif exchange == "bybit":
        if "spot" in config["market_type"]:
            raise Exception("spot not implemented on bybit")
        bot = await create_bybit_bot(tmp_live_settings)
        settings_from_exchange["maker_fee"] = 0.0001
        settings_from_exchange["taker_fee"] = 0.0006
        settings_from_exchange["exchange"] = "bybit"
    elif exchange == "bitget":
        if "spot" in config["market_type"]:
            raise Exception("spot not implemented on bitget")
        bot = await create_bitget_bot(tmp_live_settings)
        settings_from_exchange["maker_fee"] = 0.0002
        settings_from_exchange["taker_fee"] = 0.0006
        settings_from_exchange["exchange"] = "bitget"
    elif exchange == "okx":
        if "spot" in config["market_type"]:
            raise Exception("spot not implemented on okx")
        bot = await create_okx_bot(tmp_live_settings)
        settings_from_exchange["maker_fee"] = 0.0002
        settings_from_exchange["taker_fee"] = 0.0005
        settings_from_exchange["exchange"] = "okx"
    else:
        raise Exception(f"unknown exchange {exchange}")
    if hasattr(bot, "session"):
        await bot.session.close()
    if "inverse" in bot.market_type:
        settings_from_exchange["inverse"] = True
    elif any(x in bot.market_type for x in ["linear", "spot"]):
        settings_from_exchange["inverse"] = False
    else:
        raise Exception("unknown market type")
    for key in [
        "max_leverage",
        "min_qty",
        "min_cost",
        "qty_step",
        "price_step",
        "max_leverage",
        "c_mult",
        "hedge_mode",
    ]:
        settings_from_exchange[key] = getattr(bot, key)
    return settings_from_exchange


async def create_binance_bot(config: dict):
    from exchanges.binance import BinanceBot

    bot = BinanceBot(config)
    await bot._init()
    return bot


async def create_binance_bot_spot(config: dict):
    from exchanges.binance_spot import BinanceBotSpot

    bot = BinanceBotSpot(config)
    await bot._init()
    return bot


async def create_bybit_bot(config: dict):
    from exchanges.bybit import BybitBot

    bot = BybitBot(config)
    await bot._init()
    return bot


async def create_bybit_bot_spot(config: dict):
    from exchanges.bybit_spot import BybitBotSpot

    bot = BybitBotSpot(config)
    await bot._init()
    return bot


async def create_bitget_bot(config: dict):
    from exchanges.bitget import BitgetBot

    bot = BitgetBot(config)
    await bot._init()
    return bot


async def create_okx_bot(config: dict):
    from exchanges.okx import OKXBot

    bot = OKXBot(config)
    await bot._init()
    return bot


async def create_kucoin_bot(config: dict):
    from exchanges.kucoin import KuCoinBot

    bot = KuCoinBot(config)
    await bot._init()
    return bot


def add_argparse_args(parser):
    parser.add_argument("--nojit", help="disable numba", action="store_true")
    parser.add_argument(
        "-b",
        "--backtest_config",
        type=str,
        required=False,
        dest="backtest_config_path",
        default="configs/backtest/default.hjson",
        help="backtest config hjson file",
    )
    parser.add_argument(
        "-s",
        "--symbols",
        type=str,
        required=False,
        dest="symbols",
        default=None,
        help="specify symbol(s), overriding symbol from backtest config.  "
        + "multiple symbols separated with comma",
    )
    parser.add_argument(
        "-u",
        "--user",
        type=str,
        required=False,
        dest="user",
        default=None,
        help="specify user, a.k.a. account_name, overriding user from backtest config",
    )
    parser.add_argument(
        "-sd",
        "--start_date",
        type=str,
        required=False,
        dest="start_date",
        default=None,
        help="specify start date, overriding value from backtest config",
    )
    parser.add_argument(
        "-ed",
        "--end_date",
        type=str,
        required=False,
        dest="end_date",
        default=None,
        help="specify end date, overriding value from backtest config",
    )
    parser.add_argument(
        "-sb",
        "--starting_balance",
        "--starting-balance",
        type=float,
        required=False,
        dest="starting_balance",
        default=None,
        help="specify starting_balance, overriding value from backtest config",
    )
    parser.add_argument(
        "-m",
        "--market_type",
        type=str,
        required=False,
        dest="market_type",
        default=None,
        help="specify whether spot or futures (default), overriding value from backtest config",
    )
    parser.add_argument(
        "-bd",
        "--base_dir",
        type=str,
        required=False,
        dest="base_dir",
        default=None,
        help="specify the base output directory for the results",
    )
    parser.add_argument(
        "-oh",
        "--ohlcv",
        type=str,
        required=False,
        dest="ohlcv",
        default=None,
        nargs="?",
        const="y",
        help="if no arg or [y/yes], use 1m ohlcv instead of 1s ticks, overriding param ohlcv from config/backtest/default.hjson",
    )
    return parser


def get_starting_configs(config) -> [dict]:
    starting_configs = []
    if config["starting_configs"] is not None:
        try:
            if os.path.isdir(config["starting_configs"]):
                starting_configs = [
                    json.load(open(f))
                    for f in glob.glob(os.path.join(config["starting_configs"], "*.json"))
                ]
                print("Starting with all configurations in directory.")
            else:
                starting_configs = [json.load(open(config["starting_configs"]))]
                print("Starting with specified configuration.")
        except Exception as e:
            print("Could not find specified configuration.", e)
    return starting_configs


def utc_ms() -> float:
    return datetime.utcnow().timestamp() * 1000


def local_time() -> float:
    return datetime.now().astimezone().timestamp() * 1000


def get_file_mod_utc(filepath):
    """
    Get the UTC timestamp of the last modification of a file.

    Args:
        filepath (str): The path to the file.

    Returns:
        float: The UTC timestamp in milliseconds of the last modification of the file.
    """
    # Get the last modification time of the file in seconds since the epoch
    mod_time_epoch = os.path.getmtime(filepath)

    # Convert the timestamp to a UTC datetime object
    mod_time_utc = datetime.utcfromtimestamp(mod_time_epoch)

    # Return the UTC timestamp
    return mod_time_utc.timestamp() * 1000


def print_async_exception(coro):
    if isinstance(coro, list):
        for elm in coro:
            print_async_exception(elm)
    try:
        print(f"result: {coro.result()}")
    except:
        pass
    try:
        print(f"exception: {coro.exception()}")
    except:
        pass
    try:
        print(f"returned: {coro}")
    except:
        pass


async def get_first_timestamps_unified(coins: [str]):
    # returns earliest timestamp coin was found on any exchange
    # exchanges to check: binance, bybit, okx, hyperliquid, gateio
    async def get_first_timestamps_single(exchange, symbol, cc):
        if exchange in ["binanceusdm"]:
            result = await cc.fetch_ohlcv(symbol, since=1, timeframe="1d")
        elif exchange in ["bybit", "gateio"]:
            result = await cc.fetch_ohlcv(
                symbol, since=int(date2ts_utc("2018-01-01")), timeframe="1d"
            )
        elif exchange in ["okx"]:
            result = await cc.fetch_ohlcv(
                symbol, since=int(date2ts_utc("2018-01-01")), timeframe="1M"
            )
        elif exchange in ["bitget"]:
            result = await cc.fetch_ohlcv(
                symbol, since=int(date2ts_utc("2018-01-01")), timeframe="1w"
            )
        else:
            result = await cc.fetch_ohlcv(
                symbol, since=int(date2ts_utc("2021-01-01")), timeframe="1w"
            )
        return result

    cache_fpath = "caches/first_ohlcv_timestamps_unified.json"
    ftss = {}
    if os.path.exists(cache_fpath):
        try:
            ftss = json.load(open("caches/first_ohlcv_timestamps_unified.json"))
            print(f"loaded from cache {cache_fpath}")
        except Exception as e:
            print(f"Error reading {cache_fpath}")
    if all([coin in ftss for coin in coins]):
        return ftss
    missing = {c for c in coins if c not in ftss}
    print("missing", missing)
    import ccxt.async_support as ccxt

    ccxts = {}
    exchanges = {
        "okx": "USDT",
        "binanceusdm": "USDT",
        "bybit": "USDT",
        "gateio": "USDT",
        "bitget": "USDT",
        "hyperliquid": "USDC",
    }
    for exchange in exchanges:
        ccxts[exchange] = getattr(ccxt, exchange)()
        ccxts[exchange].options["defaultType"] = "swap"

    print("loading markets...")
    await asyncio.gather(*[ccxts[e].load_markets() for e in ccxts])
    tasks = {}
    for coin in missing:
        tasks[coin] = {}
        for exchange in exchanges:
            print("b", exchange, coin)
            eligible_symbols = [
                s for s in ccxts[exchange].markets if ccxts[exchange].markets[s]["swap"]
            ]
            symbol = coin_to_symbol(
                coin, eligible_symbols=eligible_symbols, quote=exchanges[exchange]
            )
            if symbol:
                tasks[coin][exchange] = asyncio.create_task(
                    get_first_timestamps_single(exchange, symbol, ccxts[exchange])
                )
    results = {}
    for coin in missing:
        results[coin] = {}
        for exchange in exchanges:
            if coin in tasks and exchange in tasks[coin]:
                try:
                    results[coin][exchange] = await tasks[coin][exchange]
                    print(
                        "c",
                        exchange,
                        coin,
                        results[coin][exchange][0] if results[coin][exchange] else "",
                    )
                except Exception as e:
                    print(f"error {exchange} {coin} {e}")
        if results[coin]:
            ftss[coin] = min(
                [fts for v in results[coin].values() if v and (fts := v[0][0]) > 1262304000000.0]
            )
        else:
            print(f"no first timestamp for {coin}")
            ftss[coin] = 0.0
    json.dump(ftss, open(cache_fpath, "w"))
    await asyncio.gather(*[ccxts[e].close() for e in ccxts])
    return ftss


async def get_first_ohlcv_timestamps_new(symbols=None, exchange="binance"):
    supported_exchanges = {
        "binance": "binanceusdm",
        "binanceusdm": "binanceusdm",
        "bybit": "bybit",
        "bitget": "bitget",
        "okx": "okx",
        "hyperliquid": "hyperliquid",
        "gateio": "gateio",
    }
    assert (
        exchange in supported_exchanges
    ), f"exchange {exchange} not in supported_exchanges {sorted(supported_exchanges)}"
    cache_fname = f"caches/first_ohlcv_timestamps_{exchange}.json"
    ftss = {}
    try:
        if os.path.exists(cache_fname):
            ftss = json.load(open(cache_fname))
    except Exception as e:
        print(f"failed to load {cache_fname} {e}")
    if isinstance(symbols, str):
        if symbols in ftss:
            return ftss[symbols]
        else:
            symbols = [symbols]
    elif isinstance(symbols, list):
        if all([s in ftss for s in symbols]):
            return {k: v for k, v in ftss.items() if k in symbols}
    import ccxt.async_support as ccxt

    cc = getattr(ccxt, supported_exchanges[exchange])()
    try:
        markets = await cc.load_markets()
        if symbols is None:
            symbols = [x for x in markets if markets[x]["swap"]]
        symbols.sort()
        to_fetch = [s for s in symbols if s not in ftss]
        if to_fetch:
            fetched = []
            since = int(date_to_ts2("2015-01-01"))
            n_concurrent = 20
            for i, symbol in enumerate(to_fetch):
                if cc.id in ["bybit", "binanceusdm"]:
                    coro = cc.fetch_ohlcv(symbol, timeframe="1d", since=since)
                else:
                    timeframe_ = "1w" if cc.id in ["hyperliquid", "gateio"] else "1M"
                    coro = cc.fetch_ohlcv(symbol, timeframe=timeframe_)
                fetched.append((symbol, asyncio.ensure_future(coro)))

                if i + 1 == len(to_fetch) or (i + 1) % n_concurrent == 0:
                    for sym, task in fetched:
                        try:
                            res = await task
                            ftss[sym] = res[0][0]
                        except Exception as e:
                            print(f"Error fetching ohlcvs for {sym} {e}")
                            if "The symbol has been removed" in str(e):
                                ftss[sym] = 0
                    try:
                        make_get_filepath(cache_fname)
                        json.dump(ftss, open(cache_fname, "w"), indent=4, sort_keys=True)
                        syms = [x[0] for x in fetched]
                        print(f"Dumped first ohlcv timestamp, {cc.id}: {','.join(syms)}")
                    except Exception as e:
                        print(f"Error dumping ohlcv first timestamps {cc.id} {e}")
                    fetched = []
                    await asyncio.sleep(1)
    finally:
        await cc.close()
    return ftss


async def get_first_ohlcv_timestamps(cc=None, symbols=None, cache=True):
    supported_exchanges = [
        "binanceusdm",
        "bybit",
        "bitget",
        "okx",
        "hyperliquid",
        "gateio",
    ]
    default_exchange = "binanceusdm"
    if symbols is not None and cc is None:
        cache_fname = f"caches/first_ohlcv_timestamps_{default_exchange}.json"
        try:
            first_timestamps = json.load(open(cache_fname))
            if all([symbol in first_timestamps for symbol in symbols]):
                return first_timestamps
        except:
            pass
    if cc is None:
        import ccxt.async_support as ccxt

        cc = ccxt.binanceusdm()
    else:
        if cc.id not in supported_exchanges:
            print(f"get_first_ohlcv_timestamps() currently only supports {supported_exchanges}")
            return {}
    try:
        if symbols is None:
            markets = await cc.load_markets()
            symbols = [x for x in markets if markets[x]["swap"] and markets[x]["active"]]
        symbols.sort()
        n = 30
        first_timestamps = {}
        cache_fname = f"caches/first_ohlcv_timestamps_{cc.id}.json"
        if cache:
            if os.path.exists(cache_fname):
                try:
                    first_timestamps = json.load(open(cache_fname))
                    symbols = [s for s in symbols if s not in first_timestamps]
                except Exception as e:
                    print(f"error loading ohlcv first ts cache", e)
        fetched = []
        for i, symbol in enumerate(symbols):
            if cc.id in ["bybit", "binanceusdm"]:
                fetched.append(
                    (
                        symbol,
                        asyncio.ensure_future(
                            cc.fetch_ohlcv(
                                symbol, timeframe="1d", since=int(date2ts_utc("2015-01-01"))
                            )
                        ),
                    )
                )
            else:
                if cc.id in ["hyperliquid", "gateio"]:
                    timeframe_ = "1w"
                else:
                    timeframe_ = "1M"
                fetched.append(
                    (symbol, asyncio.ensure_future(cc.fetch_ohlcv(symbol, timeframe=timeframe_)))
                )
            if i + 1 == len(symbols) or (i + 1) % n == 0:
                for sym, task in fetched:
                    try:
                        res = await task
                        first_timestamps[sym] = res[0][0]
                    except Exception as e:
                        print(f"error fetching ohlcvs for {sym} {e}")
                        if "The symbol has been removed" in str(e):
                            first_timestamps[sym] = 0
                if cache:
                    try:
                        make_get_filepath(cache_fname)
                        json.dump(first_timestamps, open(cache_fname, "w"), indent=4, sort_keys=True)
                        print(
                            f"dumped first ohlcv timestamp cache for {cc.id} {[x[0] for x in fetched]}"
                        )
                    except Exception as e:
                        print(f"error dumping ohlcv first timestamps cache", e)
                fetched = []
                await asyncio.sleep(1)
    finally:
        await cc.close()
    return first_timestamps


def assert_correct_ccxt_version(version=None, ccxt=None):
    if version is None:
        version = load_ccxt_version()
    if ccxt is None:
        import ccxt

    assert (
        ccxt.__version__ == version
    ), f"Currently ccxt {ccxt.__version__} is installed. Please pip reinstall requirements.txt or install ccxt v{version} manually"


def load_ccxt_version():
    try:
        # Get the directory of the current script
        script_dir = os.path.dirname(os.path.abspath(__file__))
        # Construct the path to the requirements.txt file
        requirements_path = os.path.join(script_dir, "..", "requirements.txt")

        # Open and read the requirements.txt file
        with open(requirements_path, "r") as f:
            lines = f.readlines()

        # Find the line with 'ccxt' and extract the version number
        ccxt_line = [line for line in lines if "ccxt" in line][0].strip()
        return ccxt_line[ccxt_line.find("==") + 2 :]
    except Exception as e:
        print(f"failed to load ccxt version {e}")
        return None


def fetch_market_specific_settings_multi(symbols=None, exchange="binance"):
    import ccxt

    assert_correct_ccxt_version(ccxt=ccxt)

    exchange_map = {
        # "kucoin": "kucoinfutures",
        # "okx": "okx",
        "bybit": "bybit",
        "binance": "binanceusdm",
        # "bitget": "bitget",
    }
    cc = getattr(ccxt, exchange_map[exchange])()
    cc.options["defaultType"] = "swap"
    info = cc.load_markets()
    for symbol in info:
        if exchange == "binance":
            for felm in info[symbol]["info"]["filters"]:
                if felm["filterType"] == "PRICE_FILTER":
                    info[symbol]["price_step"] = float(felm["tickSize"])
                elif felm["filterType"] == "MARKET_LOT_SIZE":
                    info[symbol]["qty_step"] = float(felm["stepSize"])
            info[symbol]["c_mult"] = info[symbol]["contractSize"]
            info[symbol]["min_cost"] = info[symbol]["limits"]["cost"]["min"]
            info[symbol]["min_qty"] = info[symbol]["limits"]["amount"]["min"]
        elif exchange == "bybit":
            info[symbol]["price_step"] = info[symbol]["precision"]["price"]
            info[symbol]["qty_step"] = info[symbol]["precision"]["amount"]
            info[symbol]["c_mult"] = info[symbol]["contractSize"]
            info[symbol]["min_cost"] = 0.0
            info[symbol]["min_qty"] = info[symbol]["limits"]["amount"]["min"]
            # ccxt reports incorrect fees for bybit perps
            info[symbol]["maker"] = info[symbol]["maker_fee"] = 0.0002
            info[symbol]["taker"] = info[symbol]["taker_fee"] = 0.00055
    for symbol in sorted(info):
        info[info[symbol]["id"]] = info[symbol]
    return info if symbols is None else {symbol: info[symbol] for symbol in symbols}


def fetch_market_specific_settings(config: dict):
    import ccxt

    assert_correct_ccxt_version(ccxt=ccxt)
    exchange = config["exchange"]
    symbol = config["symbol"]
    market_type = config["market_type"]

    settings_from_exchange = {"exchange": exchange}
    if exchange == "binance":
        if "futures" in market_type:
            if symbol.endswith("USDT") or symbol.endswith("BUSD"):
                cc = ccxt.binanceusdm()
                settings_from_exchange["inverse"] = False

            elif symbol.endswith("PERP"):
                cc = ccxt.binancecoinm()
                settings_from_exchange["inverse"] = True
            else:
                raise Exception(f"unknown symbol {symbol}")
            settings_from_exchange["hedge_mode"] = True
            settings_from_exchange["spot"] = False

        elif "spot" in market_type:
            cc = ccxt.binance()
            settings_from_exchange["spot"] = True
            settings_from_exchange["inverse"] = False
            settings_from_exchange["hedge_mode"] = False
        else:
            raise Exception(f"unknown market type {market_type}")
        markets = cc.fetch_markets()
        for elm in markets:
            if elm["id"] == symbol:
                break
        else:
            raise Exception(f"unknown symbol {symbol}")
        settings_from_exchange["maker_fee"] = elm["maker"]
        settings_from_exchange["taker_fee"] = elm["taker"]
        settings_from_exchange["c_mult"] = 1.0 if elm["contractSize"] is None else elm["contractSize"]
        settings_from_exchange["min_qty"] = elm["limits"]["amount"]["min"]
        for elm1 in elm["info"]["filters"]:
            if elm1["filterType"] == "LOT_SIZE":
                settings_from_exchange["qty_step"] = float(elm1["stepSize"])
            if elm1["filterType"] == "PRICE_FILTER":
                settings_from_exchange["price_step"] = float(elm1["tickSize"])
    elif exchange == "bitget":
        cc = ccxt.bitget()
        cc.options["defaultType"] = "swap"
        markets = cc.fetch_markets()
        for elm in markets:
            if elm["id"] == symbol and elm["swap"]:
                break
        else:
            raise Exception(f"unknown symbol {symbol}")
        settings_from_exchange["hedge_mode"] = True
        settings_from_exchange["maker_fee"] = elm["maker"]
        settings_from_exchange["taker_fee"] = elm["taker"]
        settings_from_exchange["c_mult"] = 1.0
        settings_from_exchange["price_step"] = elm["precision"]["price"]
        settings_from_exchange["qty_step"] = elm["precision"]["amount"]
        settings_from_exchange["min_qty"] = max(
            elm["limits"]["amount"]["min"], elm["precision"]["amount"]
        )
        settings_from_exchange["min_cost"] = elm["limits"]["cost"]["min"]
        settings_from_exchange["spot"] = elm["spot"]
        settings_from_exchange["inverse"] = elm["linear"] is not None and not elm["linear"]
    elif exchange == "okx":
        cc = ccxt.okx()
        markets = cc.fetch_markets()
        for elm in markets:
            if elm["type"] == "swap" and symbol in elm["id"].replace("-", ""):
                break
        else:
            raise Exception(f"unknown symbol {symbol}")
        settings_from_exchange["hedge_mode"] = True
        settings_from_exchange["maker_fee"] = elm["maker"]
        settings_from_exchange["taker_fee"] = elm["taker"]
        settings_from_exchange["c_mult"] = elm["contractSize"]
        settings_from_exchange["qty_step"] = elm["precision"]["amount"]
        settings_from_exchange["price_step"] = elm["precision"]["price"]
        settings_from_exchange["spot"] = elm["spot"]
        settings_from_exchange["inverse"] = elm["linear"] is not None and not elm["linear"]
        settings_from_exchange["min_qty"] = elm["limits"]["amount"]["min"]
    elif exchange == "bybit":
        cc = ccxt.bybit()
        markets = cc.fetch_markets()
        spot = market_type == "spot"
        for elm in markets:
            if elm["id"] == symbol and elm["spot"] == spot:
                break
        else:
            raise Exception(f"unknown symbol {symbol}")
        settings_from_exchange["hedge_mode"] = not spot
        # ccxt reports incorrect fees for bybit perps
        settings_from_exchange["maker_fee"] = 0.0002 if not spot else elm["maker"]
        settings_from_exchange["taker_fee"] = 0.00055 if not spot else elm["taker"]
        settings_from_exchange["c_mult"] = 1.0 if elm["contractSize"] is None else elm["contractSize"]
        settings_from_exchange["qty_step"] = elm["precision"]["amount"]
        settings_from_exchange["price_step"] = elm["precision"]["price"]
        settings_from_exchange["spot"] = spot
        settings_from_exchange["inverse"] = elm["linear"] is not None and not elm["linear"]
        settings_from_exchange["min_qty"] = elm["limits"]["amount"]["min"]
    elif exchange == "kucoin":
        cc = ccxt.kucoinfutures()
        markets = cc.fetch_markets()
        for elm in markets:
            if elm["id"] == symbol + "M":
                break
        else:
            raise Exception(f"unknown symbol {symbol}")
        settings_from_exchange["hedge_mode"] = True
        settings_from_exchange["maker_fee"] = elm["maker"]
        settings_from_exchange["taker_fee"] = elm["taker"]
        settings_from_exchange["c_mult"] = elm["contractSize"]
        settings_from_exchange["qty_step"] = elm["precision"]["amount"]
        settings_from_exchange["price_step"] = elm["precision"]["price"]
        settings_from_exchange["spot"] = False
        settings_from_exchange["inverse"] = elm["linear"] is not None and not elm["linear"]
        settings_from_exchange["min_qty"] = (
            0.0 if elm["limits"]["amount"]["min"] is None else elm["limits"]["amount"]["min"]
        )
        settings_from_exchange["min_qty"] = float(elm["info"]["lotSize"])
    else:
        raise Exception(f"unknown exchange {exchange}")
    if "min_cost" not in settings_from_exchange:
        settings_from_exchange["min_cost"] = (
            0.0 if elm["limits"]["cost"]["min"] is None else elm["limits"]["cost"]["min"]
        )
    for key in [
        "c_mult",
        "exchange",
        "hedge_mode",
        "inverse",
        "maker_fee",
        "min_cost",
        "min_qty",
        "price_step",
        "qty_step",
        "spot",
        "taker_fee",
    ]:
        assert key in settings_from_exchange, f"missing {key}"
    # import pprint
    # pprint.pprint(elm)
    return sort_dict_keys(settings_from_exchange)


def create_acronym(full_name, acronyms=set()):
    i = 1
    while True:
        i += 1
        if i > 100:
            raise Exception(f"too many acronym duplicates {acronym}")
            break
        shortened_name = full_name
        for k in [
            "backtest_",
            "live_",
            "optimize_bounds_",
            "optimize_limits_lower_bound_",
            "optimize_",
            "bot_",
        ]:
            if full_name.startswith(k):
                shortened_name = full_name.replace(k, "")
                break
        acronym = "".join(word[0] for word in shortened_name.split("_"))
        if acronym not in acronyms:
            break
        acronym += str(i)
        if acronym not in acronyms:
            break
    return acronym


def comma_separated_values(x):
    return x.split(",")


def comma_separated_values_float(x):
    return [float(z) for z in x.split(",")]


def add_arguments_recursively(parser, config, prefix="", acronyms=set()):

    for key, value in config.items():
        full_name = f"{prefix}{key}"

        if isinstance(value, dict):
            add_arguments_recursively(parser, value, f"{full_name}_", acronyms=acronyms)
        else:
            acronym = create_acronym(full_name, acronyms)
            appendix = ""
            type_ = type(value)
            if "bounds" in full_name:
                type_ = comma_separated_values_float
            elif "approved_coins" in full_name:
                acronym = "s"
                type_ = comma_separated_values
            elif any([x in full_name for x in ["ignored_coins", "exchanges"]]):
                type_ = comma_separated_values
                appendix = "item1,item2,item3,..."
            elif "optimize_scoring" in full_name:
                type_ = comma_separated_values
                acronym = "os"
                appendix = "Examples: adg,sharpe_ratio; mdg,sortino_ratio; ..."
            elif "cpus" in full_name:
                acronym = "c"
            elif "iters" in full_name:
                acronym = "i"
            elif type_ == bool:
                type_ = str2bool
                appendix = "[y/n]"
            parser.add_argument(
                f"--{full_name}",
                f"-{acronym}",
                type=type_,
                dest=full_name,
                required=False,
                default=None,
                metavar="",
                help=f"Override {full_name}: {str(type_.__name__)} " + appendix,
            )
            acronyms.add(acronym)


def recursive_config_update(config, key, value, path=None):
    if path is None:
        path = []

    if key in config:
        if value != config[key]:
            full_path = ".".join(path + [key])
            print(f"changed {full_path} {config[key]} -> {value}")
            config[key] = value
        return True

    key_split = key.split("_")
    if key_split[0] in config:
        new_path = path + [key_split[0]]
        return recursive_config_update(config[key_split[0]], "_".join(key_split[1:]), value, new_path)

    return False


def update_config_with_args(config, args):
    for key, value in vars(args).items():
        if value is not None:
            recursive_config_update(config, key, value)


def read_external_coins_lists(filepath) -> dict:
    """
    reads filepath and returns dict {'long': [str], 'short': [str]}
    """
    try:
        content = hjson.load(open(filepath))
        if isinstance(content, list) and all([isinstance(x, str) for x in content]):
            return {"long": content, "short": content}
        elif isinstance(content, dict):
            if all(
                [
                    pside in content
                    and isinstance(content[pside], list)
                    and all([isinstance(x, str) for x in content[pside]])
                    for pside in ["long", "short"]
                ]
            ):
                return content
    except:
        pass
    with open(filepath, "r") as file:
        content = file.read().strip()
    # Check if the content is in list format
    if content.startswith("[") and content.endswith("]"):
        # Remove brackets and split by comma
        items = content[1:-1].split(",")
        # Remove quotes and whitespace
        items = [item.strip().strip("\"'") for item in items if item.strip()]
    elif all(
        line.strip().startswith('"') and line.strip().endswith('"')
        for line in content.split("\n")
        if line.strip()
    ):
        # Split by newline, remove quotes and whitespace
        items = [line.strip().strip("\"'") for line in content.split("\n") if line.strip()]
    else:
        # Split by newline, comma, and/or space, and filter out empty strings
        items = [item.strip() for item in content.replace(",", " ").split() if item.strip()]
    return {"long": items, "short": items}


def get_size(obj: Any, seen: Set = None) -> int:
    """
    Recursively calculate size of object and its contents in bytes.

    Args:
        obj: The object to calculate size for
        seen: Set of object ids already seen (for handling circular references)

    Returns:
        Total size in bytes
    """
    # Initialize the set of seen objects if this is the top-level call
    if seen is None:
        seen = set()

    # Get object id to handle circular references
    obj_id = id(obj)

    # If object has been seen, don't count it again
    if obj_id in seen:
        return 0

    # Add this object to seen
    seen.add(obj_id)

    # Get basic size of object
    size = sys.getsizeof(obj)

    # Handle different types of containers
    if isinstance(obj, (str, bytes, bytearray)):
        pass  # Basic size already includes contents

    elif isinstance(obj, (tuple, list, set, frozenset)):
        size += sum(get_size(item, seen) for item in obj)

    elif isinstance(obj, dict):
        size += sum(get_size(k, seen) + get_size(v, seen) for k, v in obj.items())

    elif hasattr(obj, "__dict__"):
        # Add size of all attributes for custom objects
        size += get_size(obj.__dict__, seen)

    elif hasattr(obj, "__slots__"):
        # Handle objects using __slots__
        size += sum(
            get_size(getattr(obj, attr), seen) for attr in obj.__slots__ if hasattr(obj, attr)
        )

    return size


def format_size(size_bytes: int) -> str:
    """
    Format byte size into human readable string.

    Args:
        size_bytes: Size in bytes

    Returns:
        Formatted string like '1.23 MB'
    """
    for unit in ["B", "KB", "MB", "GB", "TB"]:
        if size_bytes < 1024:
            return f"{size_bytes:.2f} {unit}"
        size_bytes /= 1024
    return f"{size_bytes:.2f} PB"


def main():
    mssm = fetch_market_specific_settings_multi(exchange="bybit")
    # pprint.pprint(mssm)
    return
    """
    cfg = {"exchange": "bybit", "symbol": "DOGEUSDT", "market_type": "spot"}
    mss = fetch_market_specific_settings(cfg)
    pprint.pprint(mss)
    return
    """
    # for exchange in ["bitget"]:
    for exchange in ["kucoin", "bitget", "binance", "bybit", "okx"]:
        cfg = {"exchange": exchange, "symbol": "ETHUSDT", "market_type": "futures"}
        try:
            mss = fetch_market_specific_settings(cfg)
            print(mss)
        except:
            traceback.print_exc()


if __name__ == "__main__":
    main()

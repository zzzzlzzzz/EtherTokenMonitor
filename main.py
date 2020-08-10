from contextlib import suppress
from os import environ
from threading import Lock
from pickle import PickleError, load, dump
from decimal import Decimal
from time import sleep
from typing import Mapping, Any, Sequence, Tuple

import logging
logging.basicConfig(format='%(asctime)s - %(name)s - %(levelname)s - %(message)s', level=logging.INFO)

from telegram.bot import Bot
from telegram.utils.request import Request
from telegram.update import Update
from telegram.ext import MessageQueue, Updater, CallbackContext
from telegram.ext.messagequeue import queuedmessage

import requests

from jackfruit import *


class MainMenu(MenuView):
    text = 'Ethereum Token Monitor'
    menu_items = [
        [('Add wallet to monitor', 'AddWalletEnterContractAddress'), ],
        [('List & Remove wallet from monitor', 'ListContracts'), ],
    ]


class AddWalletEnterContractAddress(TextDataInputView):
    text = 'Enter Contact Address'

    def process_data(self, state: Mapping[str, 'GenericView'], update: 'Update', context: 'CallbackContext',
                     data: Any) -> str:
        context.chat_data['contract_address'] = data
        return 'AddWalletEnterTokenAddress'


class AddWalletEnterTokenAddress(TextDataInputView):
    text = 'Enter Token Address'

    def __init__(self, data):
        self._data = data

    def process_data(self, state: Mapping[str, 'GenericView'], update: 'Update', context: 'CallbackContext',
                     data: Any) -> str:
        user_data = self._data.setdefault(update.effective_chat.id, dict())
        token_data = user_data.setdefault(context.chat_data['contract_address'], dict())
        token_data[data] = None
        return 'MainMenu'


class ListContracts(MenuView):
    text = 'Now you monitoring this contracts: '

    menu_items = [
        [('Back', 'MainMenu'), ],
    ]

    def __init__(self, data):
        self._data = data

    def get_menu_items(self, update: 'Update', context: 'CallbackContext') -> Sequence[Sequence[Tuple[str, str]]]:
        buttons = [[(contract, '-{}'.format(contract)), ]
                   for contract in self._data.get(update.effective_chat.id, dict())]
        return [*buttons, *super().get_menu_items(update, context)]

    def process_data(self, state: Mapping[str, 'GenericView'], update: 'Update', context: 'CallbackContext', data: str,
                     msg_id: int = None) -> str:
        context.chat_data['contract_address'] = data[1:]
        return 'ListTokens'


class ListTokens(MenuView):
    menu_items = [
        [('Back', 'ListContracts'), ],
    ]

    def __init__(self, data):
        self._data = data

    def get_text(self, update: 'Update', context: 'CallbackContext') -> str:
        return 'Contract {}. You monitor this addresses: '.format(context.chat_data['contract_address'])

    def get_menu_items(self, update: 'Update', context: 'CallbackContext') -> Sequence[Sequence[Tuple[str, str]]]:
        buttons = [[(token, '-{}'.format(token)), ]
                   for token in self._data[update.effective_chat.id].get(context.chat_data['contract_address'], dict())]
        return [*buttons, *super().get_menu_items(update, context)]

    def process_data(self, state: Mapping[str, 'GenericView'], update: 'Update', context: 'CallbackContext',
                     data: str, msg_id: int = None) -> str:
        user_contracts = self._data[update.effective_chat.id]
        user_contracts[context.chat_data['contract_address']].pop(data[1:], None)
        if not user_contracts[context.chat_data['contract_address']]:
            user_contracts.pop(context.chat_data['contract_address'], None)
            return 'ListContracts'
        return 'ListTokens'


class EtherTokenMonitorBot(Bot):
    def get_balance(self, contract, address):
        url = "https://api.etherscan.io/api?module=account&action=tokenbalance&contractaddress={}&address={}&tag=latest&apikey={}"
        try:
            sleep(0.25)
            response = requests.get(url.format(contract, address, self._api_key), timeout=10)
            response.raise_for_status()
            return Decimal(response.json()['result'])
        except (requests.RequestException, ValueError, KeyError) as e:
            raise ValueError(e)

    def __init__(self, token, storage, api_key, *args, is_queued_def=True, mqueue=None, **kwargs):
        super().__init__(token, *args, **kwargs)
        self._is_messages_queued_default = is_queued_def
        self._msg_queue = mqueue or MessageQueue()

        self._storage = storage
        self._api_key = api_key
        self.data_lock = Lock()
        self.data = dict()
        """
        {
            "user_id": {
                "contract": {
                    "address": {
                        amount
                    }
                }
            }
        }
        """
        with suppress(OSError, PickleError), open(self._storage, 'rb') as f:
            self.data = load(f)

        self._main_menu = MainMenu()
        self._add_wallet_enter_contract_address = AddWalletEnterContractAddress()
        self._add_wallet_enter_token_address = AddWalletEnterTokenAddress(self.data)
        self._list_contracts = ListContracts(self.data)
        self._list_tokens = ListTokens(self.data)

    def __del__(self):
        with suppress(Exception):
            self._msg_queue.stop()

    def commit(self):
        with suppress(PickleError), open(self._storage, 'wb') as f:
            dump(self.data, f)

    @queuedmessage
    def send_message(self, *args, **kwargs):
        return super().send_message(*args, **kwargs)

    def tick(self, context):
        with self.data_lock:
            for user, contracts in self.data.items():
                for contract, addresses in contracts.items():
                    for address, amount in addresses.items():
                        with suppress(ValueError):
                            new_amount = self.get_balance(contract, address).normalize()
                            addresses[address] = new_amount
                            if amount is None:
                                context.bot.send_message(user, 'Contract {}\nAddress {}\nAmount {:f}'.
                                                         format(contract, address, new_amount))
                            elif new_amount != amount:
                                delta = (new_amount - amount).normalize()
                                context.bot.send_message(user, 'Contract {}\nAddress {}\nAmount {:f}\nDelta {:f}'.
                                                         format(contract, address, new_amount, delta))
            self.commit()

    @staticmethod
    def execute():
        bot = EtherTokenMonitorBot(environ['TOKEN'],
                                   environ.get('STORAGE', 'storage'),
                                   environ['APIKEY'],
                                   request=Request(8, environ.get('PROXY', None)),
                                   mqueue=MessageQueue())
        updater = Updater(bot=bot, use_context=True)

        class MyJackfruit(Jackfruit):
            def before_dispatch(self, update: 'Update', context: 'CallbackContext') -> None:
                bot.data_lock.acquire()

            def after_dispatch(self, update: 'Update', context: 'CallbackContext') -> None:
                bot.commit()
                bot.data_lock.release()
        MyJackfruit(updater.dispatcher, bot._main_menu, [('start', bot._main_menu.get_name())]).\
            register(bot._add_wallet_enter_contract_address,
                     bot._add_wallet_enter_token_address,
                     bot._list_contracts,
                     bot._list_tokens,
                     )

        updater.job_queue.run_repeating(bot.tick, 5 * 60)
        updater.start_polling()
        updater.idle()


if __name__ == '__main__':
    EtherTokenMonitorBot.execute()

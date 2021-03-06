# Copyright 2016 Intel Corporation
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
# ------------------------------------------------------------------------------

import logging
import random
import sys
import traceback
from collections import OrderedDict

from twisted.internet import reactor

from journal.messages.journal_transfer import BlockListRequestMessage
from journal.messages.journal_transfer import BlockListReplyMessage

from journal.messages.journal_transfer import BlockRequestMessage
from journal.messages.journal_transfer import BlockReplyMessage

from journal.messages.journal_transfer import TransactionRequestMessage
from journal.messages.journal_transfer import TransactionReplyMessage

from journal.messages.journal_transfer import UncommittedListRequestMessage
from journal.messages.journal_transfer import UncommittedListReplyMessage

from journal.messages.journal_transfer import TransferFailedMessage


logger = logging.getLogger(__name__)


def start_journal_transfer(journal, oncomplete):
    """Initiates journal transfer to peers.

    Args:
        journal (journal_core.Journal): The journal to transfer.
        oncomplete (function): The function to call when the
            journal transfer has completed.

    Returns:
        bool: Whether or not a journal transfer was initiated.
    """

    if journal.Restore is True:
        if len(journal.GlobalStoreMap.persistmap_keys()) > 1:
            # we have persistence; do not fetch remote ledger
            return False
        else:
            logger.warn('restoration from persistence layer not possible')

    transfer = JournalTransfer(journal, oncomplete)
    transfer.initiate_journal_transfer()

    return True


class JournalTransfer(object):
    """Handles the transfer of a journal to peers.

    Attributes:
        Journal (journal_core.Journal): The journal to transfer.
        Callback (function): The function to call when the
            journal transfer has completed.
    """
    def __init__(self, journal, callback):
        """Constructor for the JournalTransfer class.

        Args:
            journal (journal_core.Journal): The journal to transfer.
            callback (function): The function to call when
                the journal transfer has completed.
        """
        self.Journal = journal
        self.gossip = journal.gossip
        self.Callback = callback

    def initiate_journal_transfer(self):
        """Initiates journal transfer to peers.
        """
        if len(self.gossip.peer_list()) == 0:
            reactor.callLater(10, self.initiate_journal_transfer)
            return

        self.Peer = random.choice(self.gossip.peer_list())
        logger.info('initiate journal transfer from %s', self.Peer)

        self.BlockMap = OrderedDict()
        self.PendingBlocks = []
        self.TransactionMap = OrderedDict()
        self.PendingTransactions = []

        self.ProcessingUncommitted = False
        self.UncommittedTransactions = []

        self.Journal.dispatcher.register_message_handler(
            BlockListReplyMessage,
            self._blocklistreplyhandler)
        self.Journal.dispatcher.register_message_handler(
            BlockReplyMessage,
            self._blockreplyhandler)
        self.Journal.dispatcher.register_message_handler(
            UncommittedListReplyMessage,
            self._txnlistreplyhandler)
        self.Journal.dispatcher.register_message_handler(
            TransactionReplyMessage,
            self._txnreplyhandler)
        self.Journal.dispatcher.register_message_handler(
            TransferFailedMessage,
            self._failedhandler)

        request = BlockListRequestMessage()
        request.BlockListIndex = 0
        self.gossip.send_message(request, self.Peer.Identifier)

    def _failedhandler(self, msg, journal):
        logger.warn('journal transfer failed')

        # clear all of the message handlers
        self.Journal.dispatcher.clear_message_handler(BlockListReplyMessage)
        self.Journal.dispatcher.clear_message_handler(BlockReplyMessage)
        self.Journal.dispatcher.clear_message_handler(
            UncommittedListReplyMessage)
        self.Journal.dispatcher.clear_message_handler(TransactionReplyMessage)
        self.Journal.dispatcher.clear_message_handler(TransferFailedMessage)

        self.RetryID = reactor.callLater(10, self.initiate_journal_transfer)

    def _kick_off_next_block(self):
        """
        Check to see if there are any blocks to be received and kick off
        retrieval of the first one that doesnt already exist in our journal
        """
        while len(self.PendingBlocks) > 0:
            blockid = self.PendingBlocks.pop(0)
            if blockid not in self.Journal.BlockStore:
                request = BlockRequestMessage()
                request.BlockID = blockid
                self.gossip.send_message(request, self.Peer.Identifier)
                return True

            # copy the block information
            self.BlockMap[blockid] = self.Journal.BlockStore[blockid]

            # add all the transaction to the transaction map in order
            for txnid in self.BlockMap[blockid].TransactionIDs:
                self.TransactionMap[txnid] = None
                self.PendingTransactions.append(txnid)

        # there were no blocks, but we might have added transactions to the
        # queue to return so kick off a transaction request for the next one
        return self._kick_off_next_transaction()

    def _kick_off_next_transaction(self):
        """
        Check to see if there are any transactions to be received and kick off
        retrieval of the first one that doesnt already exist in our journal
        """
        while len(self.PendingTransactions) > 0:
            txnid = self.PendingTransactions.pop(0)
            if txnid not in self.Journal.TransactionStore:
                request = TransactionRequestMessage()
                request.TransactionID = txnid
                self.gossip.send_message(request, self.Peer.Identifier)
                return True

            self.TransactionMap[txnid] = self.Journal.TransactionStore[txnid]

        return False

    def _blocklistreplyhandler(self, msg, journal):
        logger.debug('request %s, received %d block identifiers from %s',
                     msg.InReplyTo[:8], len(msg.BlockIDs), self.Peer.Name)

        # add all the blocks to the block map in order
        for blockid in msg.BlockIDs:
            self.BlockMap[blockid] = None
            self.PendingBlocks.append(blockid)

        # if we received any block ids at all then we need to go back and ask
        # for more when no more are returned, then we know we have all of them
        if len(msg.BlockIDs) > 0:
            request = BlockListRequestMessage()
            request.BlockListIndex = msg.BlockListIndex + len(msg.BlockIDs)
            self.gossip.send_message(request, self.Peer.Identifier)
            return

        # no more block list messages, now start grabbing blocks
        if self._kick_off_next_block():
            return

        # kick off retrieval of the uncommitted transactions
        request2 = UncommittedListRequestMessage()
        request2.TransactionListIndex = 0
        self.gossip.send_message(request2, self.Peer.Identifier)

    def _txnlistreplyhandler(self, msg, journal):
        logger.debug(
            'request %s, received %d uncommitted transactions from %s',
            msg.InReplyTo[:8],
            len(msg.TransactionIDs),
            self.Peer.Name)

        # save the uncommitted transactions
        for txnid in msg.TransactionIDs:
            self.UncommittedTransactions.append(txnid)

        if len(msg.TransactionIDs) > 0:
            request = UncommittedListRequestMessage()
            request.TransactionListIndex = msg.TransactionListIndex + len(
                msg.TransactionIDs)
            self.gossip.send_message(request, self.Peer.Identifier)
            return

        # if there are no more transactions, then get the next block
        if self._kick_off_next_block():
            return

        self._handleuncommitted()

    def _blockreplyhandler(self, msg, journal):
        # leaving this as info to provide some feedback in the log for
        # ongoing progress on the journal transfer
        logger.info('request %s, received block from %s', msg.InReplyTo[:8],
                    self.Peer.Name)

        # the actual transaction block is encapsulated in a message within the
        # reply message so we need to decode it here... this is mostly to make
        # sure we have the handle to the gossiper for decoding
        btype = msg.TransactionBlockMessage['__TYPE__']
        bmessage = self.gossip.unpack_message(btype,
                                              msg.TransactionBlockMessage)

        self.BlockMap[
            bmessage.TransactionBlock.Identifier] = bmessage.TransactionBlock

        # add all the transaction to the transaction map in order
        for txnid in bmessage.TransactionBlock.TransactionIDs:
            self.TransactionMap[txnid] = None
            self.PendingTransactions.append(txnid)

        # check to see if there are any transactions
        if self._kick_off_next_transaction():
            return

        # and if there are no more transactions then
        # check to see if there are more blocks
        if self._kick_off_next_block():
            return

        self._handleuncommitted()

    def _txnreplyhandler(self, msg, journal):
        logger.debug('request %s, received transaction from %s',
                     msg.InReplyTo[:8], self.Peer.Name)

        # the actual transaction is encapsulated in a message within the reply
        # message so we need to decode it here... this is mostly to make sure
        # we have the handle to the gossiper for decoding
        ttype = msg.TransactionMessage['__TYPE__']
        tmessage = self.gossip.unpack_message(ttype, msg.TransactionMessage)

        self.TransactionMap[
            tmessage.Transaction.Identifier] = tmessage.Transaction

        # if there are more transaction pending for this block, then kick off
        # retrieval of the next one
        if self._kick_off_next_transaction():
            return

        # finished the last block, now its time to start the next one, send
        # a request for it
        if self._kick_off_next_block():
            return

        self._handleuncommitted()

    def _handleuncommitted(self):
        logger.debug('transition to uncommitted messages')

        if not self.ProcessingUncommitted and len(
                self.UncommittedTransactions) > 0:
            self.ProcessingUncommitted = True

            for txnid in self.UncommittedTransactions:
                self.TransactionMap[txnid] = None
                self.PendingTransactions.append(txnid)

            # now kick off the retrieval of the first transaction
            if self._kick_off_next_transaction():
                return

        self._finish()

    def _finish(self):
        # everything has been returned... time to update the journal,
        # first copy the transactions over and apply them to the
        # global store, then copy the blocks in

        try:
            for txnid, txn in self.TransactionMap.iteritems():
                self.Journal.add_pending_transaction(txn,
                                                     build_block=False)

            for blkid, blk in self.BlockMap.iteritems():
                self.Journal.commit_transaction_block(blk)

        except AssertionError:
            (etype, evalue, trace) = sys.exc_info()
            tbinfo = traceback.extract_tb(trace)
            (filename, line, func, text) = tbinfo[-1]
            logger.error('assertion failure in file %s at line %s', filename,
                         line)
        except:
            logger.error(
                'unexpected error happened committing blocks during journal '
                'transfer; %s',
                str(sys.exc_info()[0]))

        logger.info(
            'journal transferred from %s, %d transactions, %d blocks, current '
            'head is %s',
            self.Peer, len(self.TransactionMap), len(self.BlockMap),
            self.Journal.MostRecentCommittedBlockID[:8])

        # clear all of the message handlers
        self.Journal.dispatcher.clear_message_handler(BlockListReplyMessage)
        self.Journal.dispatcher.clear_message_handler(BlockReplyMessage)
        self.Journal.dispatcher.clear_message_handler(
            UncommittedListReplyMessage)
        self.Journal.dispatcher.clear_message_handler(TransactionReplyMessage)
        self.Journal.dispatcher.clear_message_handler(TransferFailedMessage)

        # self.RetryID.cancel()
        self.Callback()

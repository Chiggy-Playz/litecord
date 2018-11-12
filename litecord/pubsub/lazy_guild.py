"""
Main code for Lazy Guild implementation in litecord.
"""
import asyncio
from dataclasses import dataclass, asdict, field
from collections import defaultdict
from typing import Any, List, Dict, Union

from logbook import Logger

from litecord.pubsub.dispatcher import Dispatcher
from litecord.permissions import (
    Permissions, overwrite_find_mix, get_permissions, role_permissions
)
from litecord.utils import index_by_func
from litecord.storage import str_

log = Logger(__name__)

GroupID = Union[int, str]
Presence = Dict[str, Any]

# TODO: move this constant out of the lazy_guild module
MAX_ROLES = 250


@dataclass
class GroupInfo:
    """Store information about a specific group."""
    gid: GroupID
    name: str
    position: int
    permissions: Permissions


@dataclass
class MemberList:
    """Total information on the guild's member list.

    Attributes
    ----------
    groups:
        List with all group information, sorted
        by their actual position in the member list.
    data:
        Dictionary holding a list of member IDs
        for each group.
    members:
        Dictionary holding member information for
        each member in the list.
    presences:
        Dictionary holding presence data for each
        member.
    overwrites:
        Holds the channel overwrite information
        for the list (a list is tied to a single
        channel, and since only roles with Read Messages
        can be in the list, we need to store that information)
    """
    groups: List[GroupInfo] = field(default_factory=list)
    data: Dict[GroupID, List[int]] = field(default_factory=dict)
    presences: Dict[int, Presence] = field(default_factory=dict)
    members: Dict[int, Dict[str, Any]] = field(default_factory=dict)
    overwrites: Dict[int, Dict[str, Any]] = field(default_factory=dict)

    def __bool__(self):
        """Return if the current member list is fully initialized."""
        list_dict = asdict(self)

        # ignore the bool status of overwrites
        return all(bool(list_dict[k])
                   for k in ('groups', 'data', 'presences', 'members'))

    def __iter__(self):
        """Iterate over all groups in the correct order.

        Yields a tuple containing :class:`GroupInfo` and
        the List[int] for the group.
        """
        if not self.groups:
            return

        for group in self.groups:
            yield group, self.data[group.gid]

    @property
    def iter_non_empty(self):
        """Only iterate through non-empty groups"""

        for group, member_ids in self:
            count = len(member_ids)

            if group.gid == 'offline':
                yield group, member_ids
                continue

            if count == 0:
                continue

            yield group, member_ids

    @property
    def groups_complete(self):
        """Yield only group info for groups that have more than
        1 member.

        Always will output the 'offline' group.
        """
        for group, member_ids in self.iter_non_empty:
            count = len(member_ids)
            yield group, count

    @property
    def group_info(self) -> dict:
        """Return a dictionary with group information."""
        # this isn't actively used.
        return {g.gid: g for g in self.groups}

    def is_empty(self, group_id: GroupID) -> bool:
        """Return if a group is empty."""
        return len(self.data[group_id]) == 0

    def is_birth(self, group_id: GroupID) -> bool:
        """Return if a group is with a single presence."""
        return len(self.data[group_id]) == 1


@dataclass
class Operation:
    """Represents a member list operation."""
    list_op: str
    params: Dict[str, Any]

    @property
    def to_dict(self) -> dict:
        """Return a dictionary representation of the operation."""
        if self.list_op not in ('SYNC', 'INVALIDATE',
                                'INSERT', 'UPDATE', 'DELETE'):
            raise ValueError('Invalid list operator')

        res = {
            'op': self.list_op
        }

        if self.list_op == 'SYNC':
            res['items'] = self.params['items']

        if self.list_op in ('SYNC', 'INVALIDATE'):
            res['range'] = self.params['range']

        if self.list_op in ('INSERT', 'DELETE', 'UPDATE'):
            res['index'] = self.params['index']

        if self.list_op in ('INSERT', 'UPDATE'):
            res['item'] = self.params['item']

        return res


def _to_simple_group(presence: dict) -> str:
    """Return a simple group (not a role), given a presence."""
    return 'offline' if presence['status'] == 'offline' else 'online'


def display_name(member_nicks: Dict[str, str], presence: Presence) -> str:
    """Return the display name of a presence.

    Used to sort groups.
    """
    uid = presence['user']['id']

    uname = presence['user']['username']
    nick = member_nicks.get(uid)

    return nick or uname


def merge(member: dict, presence: Presence) -> dict:
    """Merge a member dictionary and a presence dictionary
    into an item."""
    return {**member, **{
        'presence': {
            'user': {
                'id': str(member['user']['id']),
            },
            'status': presence['status'],
            'game': presence['game'],
            'activities': presence['activities']
        }
    }}


class GuildMemberList:
    """This class stores the current member list information
    for a guild (by channel).

    As channels can have different sets of roles that can
    read them and so, different lists, this is more of a
    "channel member list" than a guild member list.

    Attributes
    ----------
    main_lg: LazyGuildDispatcher
        Main instance of :class:`LazyGuildDispatcher`,
        so that we're able to use things such as :class:`Storage`.
    guild_id: int
        The Guild ID this instance is referring to.
    channel_id: int
        The Channel ID this instance is referring to.
    member_list: List
        The actual member list information.
    state: set
        The set of session IDs that are subscribed to the guild.

        User IDs being used as the identifier in GuildMemberList
        is a wrong assumption. It is true Discord rolled out
        lazy guilds to all of the userbase, but users that are bots,
        for example, can still rely on PRESENCE_UPDATEs.
    """
    def __init__(self, guild_id: int,
                 channel_id: int, main_lg):
        self.guild_id = guild_id
        self.channel_id = channel_id

        self.main = main_lg
        self.list = MemberList()

        #: store the states that are subscribed to the list.
        #  type is {session_id: set[list]}
        self.state = defaultdict(set)

        self._list_lock = asyncio.Lock()

    @property
    def loop(self):
        """Get the main asyncio loop instance."""
        return self.main.app.loop

    @property
    def storage(self):
        """Get the global :class:`Storage` instance."""
        return self.main.app.storage

    @property
    def presence(self):
        """Get the global :class:`PresenceManager` instance."""
        return self.main.app.presence

    @property
    def state_man(self):
        """Get the global :class:`StateManager` instance."""
        return self.main.app.state_manager

    @property
    def list_id(self):
        """get the id of the member list."""
        return ('everyone'
                if self.channel_id == self.guild_id
                else str(self.channel_id))

    def _set_empty_list(self):
        """Set the member list as being empty."""
        self.list = MemberList(None, None, None, None)

    async def _init_check(self):
        """Check if the member list is initialized before
        messing with it."""
        if not self.list:
            await self._init_member_list()

    async def _fetch_overwrites(self):
        overwrites = await self.storage.chan_overwrites(self.channel_id)
        overwrites = {int(ov['id']): ov for ov in overwrites}
        self.list.overwrites = overwrites

    def _calc_member_group(self, roles: List[int], status: str):
        """Calculate the best fitting group for a member,
        given their roles and their current status."""
        try:
            # the first group in the list
            # that the member is entitled to is
            # the selected group for the member.
            group_id = next(g.gid for g in self.list.groups
                            if g.gid in roles)
        except StopIteration:
            # no group was found, so we fallback
            # to simple group"
            group_id = _to_simple_group({'status': status})

        return group_id

    def _can_read_chan(self, group: GroupInfo):
        # get the base role perms
        role_perms = group.permissions

        # then the final perms for that role if
        # any overwrite exists in the channel
        final_perms = overwrite_find_mix(
            role_perms, self.list.overwrites, group.gid)

        # update the group's permissions
        # with the mixed ones
        group.permissions = final_perms

        # if the role can read messages, then its
        # part of the group.
        return final_perms.bits.read_messages

    async def get_role_groups(self) -> List[GroupInfo]:
        """Get role information, but only:
         - the ID
         - the name
         - the position
         - the permissions

        of all HOISTED roles AND roles that
        have permissions to read the channel
        being referred to this :class:`GuildMemberList`
        instance.

        The list is sorted by each role's position.
        """
        roledata = await self.storage.db.fetch("""
        SELECT id, name, hoist, position, permissions
        FROM roles
        WHERE guild_id = $1
        """, self.guild_id)

        hoisted = [
            GroupInfo(row['id'], row['name'],
                      row['position'],
                      Permissions(row['permissions']))
            for row in roledata if row['hoist']
        ]

        # sort role list by position
        hoisted = sorted(hoisted, key=lambda group: group.position,
                         reverse=True)

        # we need to store the overwrites since
        # we have incoming presences to manage.
        await self._fetch_overwrites()

        return list(filter(self._can_read_chan, hoisted))

    async def set_groups(self):
        """Get the groups for the member list."""
        role_groups = await self.get_role_groups()

        # inject default groups 'online' and 'offline'
        # their position is always going to be the last ones.
        self.list.groups = role_groups + [
            GroupInfo('online', 'online', MAX_ROLES + 1, 0),
            GroupInfo('offline', 'offline', MAX_ROLES + 2, 0)
        ]

    async def get_group_for_member(self, member_id: int,
                                   roles: List[Union[str, int]],
                                   status: str) -> GroupID:
        """Return a fitting group ID for the member."""
        member_roles = list(map(int, roles))

        # get the member's permissions relative to the channel
        # (accounting for channel overwrites)
        member_perms = await get_permissions(
            member_id, self.channel_id, storage=self.storage)

        if not member_perms.bits.read_messages:
            return None

        # if the member is offline, we
        # default give them the offline group.
        group_id = ('offline' if status == 'offline'
                    else self._calc_member_group(member_roles, status))

        return group_id

    async def _list_fill_groups(self, member_ids: List[int]):
        """Fill in groups with the member ids."""
        for member_id in member_ids:
            presence = self.list.presences[member_id]

            group_id = await self.get_group_for_member(
                member_id, presence['roles'], presence['status']
            )

            member = await self.storage.get_member_data_one(
                self.guild_id, member_id
            )

            self.list.members[member_id] = member
            self.list.data[group_id].append(member_id)

    async def get_member_nicks_dict(self) -> dict:
        """Get a dictionary with nickname information."""
        members = await self.storage.get_member_data(self.guild_id)

        # make a dictionary of member ids to nicknames
        # so we don't need to keep querying the db on
        # every loop iteration
        member_nicks = {m['user']['id']: m.get('nick')
                        for m in members}

        return member_nicks

    def display_name(self, member_id: int):
        member = self.list.members.get(member_id)

        if not member_id:
            return None

        username = member['user']['username']
        nickname = member['nick']

        return nickname or username

    async def _sort_groups(self):
        for member_ids in self.list.data.values():

            # this should update the list in-place
            member_ids.sort(
                key=self.display_name)

    async def __init_member_list(self):
        """Generate the main member list with groups."""
        member_ids = await self.storage.get_member_ids(self.guild_id)

        presences = await self.presence.guild_presences(
            member_ids, self.guild_id)

        # set presences in the list
        self.list.presences = {int(p['user']['id']): p
                               for p in presences}

        await self.set_groups()

        log.debug('init: {} members, {} groups',
                  len(member_ids),
                  len(self.list.groups))

        # allocate a list per group
        self.list.data = {group.gid: [] for group in self.list.groups}

        await self._list_fill_groups(member_ids)

        # second pass: sort each group's members
        # by the display name
        await self._sort_groups()

    async def _init_member_list(self):
        try:
            await self._list_lock.acquire()
            await self.__init_member_list()
        finally:
            self._list_lock.release()

    def get_member_as_item(self, member_id: int) -> dict:
        """Get an item representing a member."""
        member = self.list.members[member_id]
        presence = self.list.presences[member_id]

        return merge(member, presence)

    @property
    def items(self) -> list:
        """Main items list."""

        # TODO: maybe make this stored in the list
        # so we don't need to keep regenning?

        if not self.list:
            return []

        res = []

        # NOTE: maybe use map()?
        for group, member_ids in self.list:

            # do not send information on groups
            # that don't have anyone
            if not member_ids:
                continue

            res.append({
                'group': {
                    'id': str(group.gid),
                    'count': len(member_ids),
                }
            })

            for member_id in member_ids:
                res.append({
                    'member': self.get_member_as_item(member_id)
                })

        return res

    async def sub(self, _session_id: str):
        """Subscribe a shard to the member list."""
        await self._init_check()

    def unsub(self, session_id: str):
        """Unsubscribe a shard from the member list"""
        try:
            self.state.pop(session_id)
        except KeyError:
            pass

        # once we reach 0 subscribers,
        # we drop the current member list we have (for memory)
        # but keep the GuildMemberList running (as
        #  uninitialized) for a future subscriber.

        if not self.state:
            self._set_empty_list()

    def get_state(self, session_id: str):
        """Get the state for a session id."""
        try:
            state = self.state_man.fetch_raw(session_id)
            return state
        except KeyError:
            return None

    async def _dispatch_sess(self, session_ids: List[str],
                             operations: List[Operation]):
        """Dispatch a GUILD_MEMBER_LIST_UPDATE to the
        given session ids."""

        # construct the payload to dispatch
        payload = {
            'id': self.list_id,
            'guild_id': str(self.guild_id),

            'groups': [
                {
                    'id': str(group.gid),
                    'count': count,
                } for group, count in self.list.groups_complete
            ],

            'ops': [
                operation.to_dict
                for operation in operations
            ]
        }

        states = map(self.get_state, session_ids)
        states = filter(lambda state: state is not None, states)

        dispatched = []

        for state in states:
            await state.ws.dispatch(
                'GUILD_MEMBER_LIST_UPDATE', payload)

            dispatched.append(state.session_id)

        return dispatched

    async def resync(self, session_ids: int, item_index: int) -> List[str]:
        """Send a SYNC event to all states that are subscribed to an item.

        Returns
        -------
        List[str]
            The list of session ids that had the SYNC operation
            resent to.
        """

        result = []

        for session_id in session_ids:
            # find the list range that the group was on
            # so we resync only the given range, instead
            # of the whole list state.
            ranges = self.state[session_id]

            try:
                # get the only range where the group is in
                role_range = next((r_min, r_max) for r_min, r_max in ranges
                                  if r_min <= item_index <= r_max)
            except StopIteration:
                log.debug('ignoring sess_id={}, no range for item {}, {}',
                          session_id, item_index, ranges)
                continue

            # do resync-ing in the background
            result.append(session_id)
            self.loop.create_task(
                self.shard_query(session_id, [role_range])
            )

        return result

    async def resync_by_item(self, item_index: int):
        """Resync but only giving the item index."""
        return await self.resync(
            self.get_subs(item_index),
            item_index
        )

    async def shard_query(self, session_id: str, ranges: list):
        """Send a GUILD_MEMBER_LIST_UPDATE event
        for a shard that is querying about the member list.

        For the purposes of documentation:
        Range = Union[List, Tuple]

        Paramteters
        -----------
        session_id: str
            The Session ID querying information.
        channel_id: int
            The Channel ID that we want information on.
        ranges: List[Range[int, int]]
            ranges of the list that we want.
        """

        # a guild list with a channel id of the guild
        # represents the 'everyone' global list.
        list_id = self.list_id

        # if everyone can read the channel,
        # we direct the request to the 'everyone' gml instance
        # instead of the current one.
        everyone_perms = await role_permissions(
            self.guild_id,
            self.guild_id,
            self.channel_id,
            storage=self.storage
        )

        if everyone_perms.bits.read_messages and list_id != 'everyone':
            everyone_gml = await self.main.get_gml(self.guild_id)

            return await everyone_gml.shard_query(
                session_id, ranges
            )

        await self._init_check()

        ops = []

        for start, end in ranges:
            itemcount = end - start

            # ignore incorrect ranges
            if itemcount < 0:
                continue

            self.state[session_id].add((start, end))

            ops.append(Operation('SYNC', {
                'range': [start, end],
                'items': self.items[start:end]
            }))

        # send SYNCs to the state that requested
        await self._dispatch_sess([session_id], ops)

    def get_item_index(self, user_id: Union[str, int]) -> int:
        """Get the item index a user is on."""
        user_id = int(user_id)
        index = 1

        for g, member_ids in self.list.iter_non_empty:
            try:
                relative_index = member_ids.index(user_id)
                index += relative_index

                return index
            except ValueError:
                pass

            # +1 is for the group item
            index += 1 + len(member_ids)

        return None

    def get_group_item_index(self, group_id: GroupID) -> int:
        """Get the item index a group is on."""
        index = 0

        for group, count in self.list.groups_complete:
            if group.gid == group_id:
                return index

            index += 1 + count

        return None

    def state_is_subbed(self, item_index, session_id: str) -> bool:
        """Return if a state's ranges include the given
        item index."""

        ranges = self.state[session_id]

        for range_start, range_end in ranges:
            if range_start <= item_index <= range_end:
                return True

        return False

    def get_subs(self, item_index: int) -> filter:
        """Get the list of subscribed states to a given item."""
        return filter(
            lambda sess_id: self.state_is_subbed(item_index, sess_id),
            self.state.keys()
        )

    async def _pres_update_simple(self, user_id: int):
        item_index = self.get_item_index(user_id)

        if item_index is None:
            log.warning('lazy guild got invalid pres update uid={}',
                        user_id)
            return []

        item = self.items[item_index]
        session_ids = self.get_subs(item_index)

        # simple update means we just give an UPDATE
        # operation
        return await self._dispatch_sess(
            session_ids,
            [
                Operation('UPDATE', {
                    'index': item_index,
                    'item': item,
                })
            ]
        )

    async def _pres_update_complex(
            self, user_id: int,
            old_group: GroupID, rel_index: int,
            new_group: GroupID):
        """Move a member between groups.

        Parameters
        ----------
        user_id:
            The user that is moving.
        old_group:
            The group the user is currently in.
        rel_index:
            The relative index of the user inside old_group's list.
        new_group:
            The group the user has to move to.
        """

        log.debug('complex update: uid={} old={} rel_idx={} new={}',
                  user_id, old_group, rel_index, new_group)

        ops = []

        old_user_index = self.get_item_index(user_id)
        old_group_index = self.get_group_item_index(old_group)

        ops.append(Operation('DELETE', {
            'index': old_user_index
        }))

        # do the necessary changes
        self.list.data[old_group].remove(user_id)
        self.list.data[new_group].append(user_id)

        await self._sort_groups()

        new_user_index = self.get_item_index(user_id)

        ops.append(Operation('INSERT', {
            'index': new_user_index,

            # TODO: maybe construct the new item manually
            # instead of resorting to items list?
            'item': self.items[new_user_index]
        }))

        # put a INSERT operation if this is
        # the first member in the group.
        if self.list.is_birth(new_group) and new_group != 'offline':
            ops.append(Operation('INSERT', {
                'index': self.get_group_item_index(new_group),
                'item': {
                    'group': str(new_group), 'count': 1
                }
            }))

        # only add DELETE for the old group after
        # both operations.
        if self.list.is_empty(old_group):
            ops.append(Operation('DELETE', {
                'index': old_group_index,
            }))

        session_ids_old = list(self.get_subs(old_user_index))
        session_ids_new = list(self.get_subs(new_user_index))
        # session_ids = set(session_ids_old + session_ids_new)

        # NOTE: this section is what a realistic implementation
        # of lazy guilds would do. i've been tackling the same issue
        # for a week without success, something alongside the indexes
        # of the UPDATE operation don't match up with the official client.

        # from now on i'm pulling a mass-SYNC for both session ids,
        # which should be handled gracefully, but then we're going off-spec.

        # return await self._dispatch_sess(
        #    session_ids,
        #    ops
        # )

        # merge both results together
        return (await self.resync(session_ids_old, old_user_index) +
                await self.resync(session_ids_new, new_user_index))

    async def pres_update(self, user_id: int,
                          partial_presence: Presence):
        """Update a presence inside the member list.

        There are 5 types of updates that can happen for a user in a group:
         - from 'offline' to any
         - from any to 'offline'
         - from any to any
         - from G to G (with G being any group), while changing position
         - from G to G (with G being any group), but not changing position

        any: 'online' | role_id

        All 1st, 2nd, 3rd, and 4th updates are 'complex' updates,
        which means we'll have to change the group the user is on
        to account for them, or we'll change the position a user
        is in inside a group (for the 4th update).

        The fifth is a 'simple' change, since we're not changing
        the group a user is on, and so there's less overhead
        involved.
        """
        await self._init_check()

        old_group = None
        old_presence = self.list.presences[user_id]
        has_nick = 'nick' in partial_presence

        # partial presences don't have 'nick'. we only use it
        # as a flag that we're doing a mixed update (complex
        # but without any inter-group changes)
        try:
            partial_presence.pop('nick')
        except KeyError:
            pass

        for group, member_ids in self.list:
            try:
                old_index = member_ids.index(user_id)
            except ValueError:
                log.debug('skipping group {}', group)
                continue

            log.debug('found index for uid={}: gid={}',
                      user_id, group.gid)

            old_group = group.gid
            break

        # if we didn't find any old group for
        # the member, then that means the member
        # wasn't in the list in the first place

        if not old_group:
            log.warning('pres update with unknown old group uid={}',
                        user_id)
            return []

        roles = partial_presence.get('roles', old_presence['roles'])
        status = partial_presence.get('status', old_presence['status'])

        # calculate a possible new group
        new_group = await self.get_group_for_member(
            user_id, roles, status)

        log.debug('pres update: gid={} cid={} old_g={} new_g={}',
                  self.guild_id, self.channel_id, old_group, new_group)

        # update our presence with the given partial presence
        # since in both cases we'd update it anyways
        self.list.presences[user_id].update(partial_presence)

        # if we're going to the same group AND there are no
        # nickname changes, treat this as a simple update
        if old_group == new_group and not has_nick:
            return await self._pres_update_simple(user_id)

        return await self._pres_update_complex(
            user_id, old_group, old_index, new_group)

    async def new_role(self, role: dict):
        """Add a new role to the list.

        Only adds the new role to the list if the role
        has the necessary permissions to start with.
        """
        if not self.list:
            return

        group_id = int(role['id'])

        new_group = GroupInfo(
            group_id, role['name'],
            role['position'], Permissions(role['permissions'])
        )

        # check if new role has good perms
        await self._fetch_overwrites()

        if not self._can_read_chan(new_group):
            log.info('ignoring incoming group {}', new_group)
            return

        log.debug('new_role: inserted rid={} (gid={}, cid={})',
                  group_id, self.guild_id, self.channel_id)

        # maintain role sorting
        self.list.groups.insert(role['position'], new_group)

        # since this is a new group, we can set it
        # as a new empty list (as nobody is in the
        # new role by default)

        # NOTE: maybe that assumption changes
        # when bots come along.
        self.list.data[new_group.gid] = []

    def _get_role_as_group_idx(self, role_id: int) -> int:
        """Get a group index representing the given role id.

        Returns
        -------
        int
            Representing the ID of the role inside the
            group list.

        None
            If any of those occour:
             - member list is uninitialized.
             - role is not found inside the group list.
        """
        if not self.list:
            log.warning('uninitialized list for gid={} cid={} rid={}',
                        self.guild_id, self.channel_id, role_id)
            return None

        groups_idx = index_by_func(
            lambda g: g.gid == role_id,
            self.list.groups
        )

        if groups_idx is None:
            log.info('ignoring rid={}, unknown group',
                     role_id)
            return None

        return groups_idx

    async def role_pos_update(self, role: dict):
        """Change a role's position if it is in the group list
        to start with.

        This resorts the entire group list, which might be
        an inefficient operation.
        """
        role_id = int(role['id'])

        old_index = self.get_group_item_index
        old_sessions = list(self.get_subs(old_index))

        groups_idx = self._get_role_as_group_idx(role_id)
        if groups_idx is None:
            log.debug('ignoring rid={} because not group (gid={}, cid={})',
                      role_id, self.guild_id, self.channel_id)
            return

        group = self.list.groups[groups_idx]
        group.position = role['position']

        # TODO: maybe this can be more efficient?
        # we could self.list.groups.insert... but I don't know.
        # I'm taking the safe route right now by using sorted()
        new_groups = sorted(self.list.groups,
                            key=lambda group: group.position,
                            reverse=True)

        log.debug('resorted groups from role pos upd '
                  'rid={} rpos={} (gid={}, cid={}) '
                  'res={}',
                  role_id, group.position, self.guild_id, self.channel_id,
                  [g.gid for g in new_groups])

        self.list.groups = new_groups
        new_index = self.get_group_item_index(role_id)

        return (await self.resync(old_sessions, old_index) +
                await self.resync_by_item(new_index))

    async def role_update(self, role: dict):
        """Update a role.

        This function only takes care of updating
        any permission-related info, and removing
        the group if it lost the permissions to
        read the channel.
        """
        if not self.list:
            return

        role_id = int(role['id'])

        group_idx = self._get_role_as_group_idx(role_id)

        if not group_idx and role['hoist']:
            # this is a new group, so we'll treat it accordingly.
            log.debug('role_update promote to new_role call rid={}',
                      role_id)
            return await self.new_role(role)

        if not group_idx:
            log.debug('role is not group {} (gid={}, cid={})',
                      role_id, self.guild_id, self.channel_id)
            return

        group = self.list.groups[group_idx]
        group.permissions = Permissions(role['permissions'])

        await self._fetch_overwrites()

        # if the role can't read the channel anymore,
        # we have to delete it just like an actual role
        # deletion event.

        # role_delte will take care of sending the
        # respective GUILD_MEMBER_LIST_UPDATE events
        # down to the subscribers.
        if not self._can_read_chan(group):
            log.debug('role_update promote to role_delete '
                      'call rid={} (lost perms)',
                      role_id)
            return await self.role_delete(role_id)

        if not role['hoist']:
            log.debug('role_update promote to role_delete '
                      'call rid={} (no hoist)',
                      role_id)
            return await self.role_delete(role_id)

    async def role_delete(self, role_id: int):
        """Called when a role is deleted, so we should
        delete it off the list and reassign presences."""
        if not self.list:
            return

        # before we delete anything, we need to find the
        # states we'll resend the list info to.

        # find the item id for the group info
        role_item_index = self.get_group_item_index(role_id)

        # we only resync when we actually have an item to resync
        # we don't have items to resync when we:
        #  - a role without users is losing hoist
        #  - the role isn't a group to begin with

        # we convert the get_subs result to a list
        # so we have all the states to resync with.

        # using a filter object would cause problems
        # as we only resync AFTER we delete the group
        sess_ids_resync = (list(self.get_subs(role_item_index))
                           if role_item_index is not None
                           else [])

        # remove the group info off the list
        groups_index = index_by_func(
            lambda group: group.gid == role_id,
            self.list.groups
        )

        if groups_index is not None:
            del self.list.groups[groups_index]
        else:
            log.warning('list unstable: {} not on group list', role_id)

        # now the data info
        try:
            # we need to reassign those orphan presences
            # into a new group
            member_ids = self.list.data.pop(role_id)

            # by calling the same functions we'd be calling
            # when generating the guild, we can reassign
            # the presences into new groups and sort
            # the new presences so we achieve the correct state
            log.debug('reassigning {} presences', len(member_ids))
            await self._list_fill_groups(
                member_ids
            )
            await self._sort_groups()
        except KeyError:
            log.warning('list unstable: {} not in data dict', role_id)

        try:
            self.list.overwrites.pop(role_id)
        except KeyError:
            # don't need to log as not having a overwrite
            # is acceptable behavior.
            pass

        # after removing, we do a resync with the
        # shards that had the group.

        log.info('role_delete rid={} (gid={}, cid={})',
                 role_id, self.guild_id, self.channel_id)

        log.debug('there are {} session ids to resync (for item {})',
                  len(sess_ids_resync), role_item_index)

        return await self.resync(sess_ids_resync, role_item_index)


class LazyGuildDispatcher(Dispatcher):
    """Main class holding the member lists for lazy guilds."""
    # channel ids
    KEY_TYPE = int

    # the session ids subscribing to channels
    VAL_TYPE = str

    def __init__(self, main):
        super().__init__(main)

        self.storage = main.app.storage

        # {chan_id: gml, ...}
        self.state = {}

        #: store which guilds have their
        #  respective GMLs
        # {guild_id: [chan_id, ...], ...}
        self.guild_map = defaultdict(list)

    async def get_gml(self, channel_id: int):
        """Get a guild list for a channel ID,
        generating it if it doesn't exist."""
        try:
            return self.state[channel_id]
        except KeyError:
            guild_id = await self.storage.guild_from_channel(
                channel_id
            )

            # if we don't find a guild, we just
            # set it the same as the channel.
            if not guild_id:
                guild_id = channel_id

            gml = GuildMemberList(guild_id, channel_id, self)
            self.state[channel_id] = gml
            self.guild_map[guild_id].append(channel_id)
            return gml

    def get_gml_guild(self, guild_id: int) -> List[GuildMemberList]:
        """Get all member lists for a given guild."""
        return list(map(
            self.state.get,
            self.guild_map[guild_id]
        ))

    async def unsub(self, chan_id, session_id):
        gml = await self.get_gml(chan_id)
        gml.unsub(session_id)

    async def dispatch(self, guild_id, event: str, *args, **kwargs):
        """Call a function specialized in handling the given event"""
        try:
            handler = getattr(self, f'_handle_{event.lower()}')
        except AttributeError:
            log.warning('unknown event: {}', event)
            return

        await handler(guild_id, *args, **kwargs)

    async def _call_all_lists(self, guild_id, method_str: str, *args):
        lists = self.get_gml_guild(guild_id)

        log.debug('calling method={} to all {} lists',
                  method_str, len(lists))

        for lazy_list in lists:
            method = getattr(lazy_list, method_str)
            await method(*args)

    async def _handle_new_role(self, guild_id: int, new_role: dict):
        """Handle the addition of a new group by dispatching it to
        the member lists."""
        await self._call_all_lists(guild_id, 'new_role', new_role)

    async def _handle_role_pos_upd(self, guild_id, role: dict):
        await self._call_all_lists(guild_id, 'role_pos_update', role)

    async def _handle_role_update(self, guild_id, role: dict):
        # handle name and hoist changes
        await self._call_all_lists(guild_id, 'role_update', role)

    async def _handle_role_delete(self, guild_id, role_id: int):
        await self._call_all_lists(guild_id, 'role_delete', role_id)

    async def _handle_pres_update(self, guild_id, user_id: int,
                                  partial: dict):
        await self._call_all_lists(
            guild_id, 'pres_update', user_id, partial)

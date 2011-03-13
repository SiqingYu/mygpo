import uuid
from datetime import datetime
from couchdbkit import ResourceNotFound
from couchdbkit.ext.django.schema import *

from mygpo.core.models import Podcast


class Rating(DocumentSchema):
    rating = IntegerProperty()
    timestamp = DateTimeProperty(default=datetime.utcnow)


class Suggestions(Document):
    user = StringProperty()
    user_oldid = IntegerProperty()
    podcasts = StringListProperty()
    blacklist = StringListProperty()
    ratings = SchemaListProperty(Rating)

    @classmethod
    def for_user_oldid(cls, oldid):
        r = cls.view('users/suggestions_by_user_oldid', key=oldid, \
            include_docs=True)
        if r:
            return r.first()
        else:
            s = Suggestions()
            s.user_oldid = oldid
            return s


    def get_podcasts(self, count=None):
        user = User.for_oldid(self.user_oldid)
        subscriptions = user.get_subscribed_podcast_ids()

        ids = filter(lambda x: not x in self.blacklist + subscriptions, self.podcasts)
        if count:
            ids = ids[:count]
        return filter(lambda x: x.title, Podcast.get_multi(ids))


    def __repr__(self):
        if not self._id:
            return super(Suggestions, self).__repr__()
        else:
            return '%d Suggestions for %s (%s)' % \
                (len(self.podcasts),
                 self.user[:10] if self.user else self.user_oldid,
                 self._id[:10])


class EpisodeAction(DocumentSchema):
    """
    One specific action to an episode. Must
    always be part of a EpisodeUserState
    """

    action        = StringProperty(required=True)
    timestamp     = DateTimeProperty(required=True)
    device_oldid  = IntegerProperty()
    started       = IntegerProperty()
    playmark      = IntegerProperty()
    total         = IntegerProperty()

    def __eq__(self, other):
        if not isinstance(other, EpisodeAction):
            return False
        vals = ('action', 'timestamp', 'device_oldid', 'started', 'playmark',
                'total')
        return all([getattr(self, v, None) == getattr(other, v, None) for v in vals])


    def __repr__(self):
        return '%s-Action on %s at %s (in %s)' % \
            (self.action, self.device_oldid, self.timestamp, self._id)


class EpisodeUserState(Document):
    """
    Contains everything a user has done with an Episode
    """

    episode_oldid = IntegerProperty()
    episode       = StringProperty(required=True)
    actions       = SchemaListProperty(EpisodeAction)
    settings      = DictProperty()
    user_oldid    = IntegerProperty()
    ref_url       = StringProperty(required=True)
    podcast_ref_url = StringProperty(required=True)


    @classmethod
    def for_user_episode(cls, user_oldid, episode_id):
        r = cls.view('users/episode_states_by_user_episode',
            key=[user_oldid, episode_id], include_docs=True)
        return r.first() if r else None


    @classmethod
    def count(cls):
        r = cls.view('users/episode_states_by_user_episode',
            limit=0)
        return r.total_rows


    def add_actions(self, actions):
        self.actions += actions
        self.actions = list(set(self.actions))
        self.actions.sort(key=lambda x: x.timestamp)


    def is_favorite(self):
        return self.settings.get('is_favorite', False)


    def set_favorite(self, set_to=True):
        self.settings['is_favorite'] = set_to


    def __repr__(self):
        return 'Episode-State %s (in %s)' % \
            (self.episode, self._id)

    def __eq__(self, other):
        if not isinstance(other, EpisodeUserState):
            return False

        return (self.episode_oldid == other.episode_oldid and \
                self.episode == other.episode and
                self.actions == other.actions)



class SubscriptionAction(Document):
    action    = StringProperty()
    timestamp = DateTimeProperty(default=datetime.utcnow)
    device    = StringProperty()


    def __cmp__(self, other):
        return cmp(self.timestamp, other.timestamp)

    def __eq__(self, other):
        return self.action == other.action and \
               self.timestamp == other.timestamp and \
               self.device == other.device

    def __hash__(self):
        return hash(self.action) + hash(self.timestamp) + hash(self.device)


class PodcastUserState(Document):
    """
    Contains everything that a user has done
    with a specific podcast and all its episodes
    """

    podcast       = StringProperty(required=True)
    episodes      = SchemaDictProperty(EpisodeUserState)
    user_oldid    = IntegerProperty()
    settings      = DictProperty()
    actions       = SchemaListProperty(SubscriptionAction)
    tags          = StringListProperty()
    ref_url       = StringProperty(required=True)
    disabled_devices = StringListProperty()


    @classmethod
    def for_user_podcast(cls, user, podcast):
        r = PodcastUserState.view('users/podcast_states_by_podcast', \
            key=[podcast.get_id(), user.id], limit=1, include_docs=True)
        if r:
            return r.first()
        else:
            p = PodcastUserState()
            p.podcast = podcast.get_id()
            p.user_oldid = user.id
            p.ref_url = podcast.url

            from mygpo import migrate
            for device in migrate.get_devices(user):
                p.set_device_state(device)

            return p


    @classmethod
    def for_user(cls, user):
        r = PodcastUserState.view('users/podcast_states_by_user',
            startkey=[user.id, None], endkey=[user.id, 'ZZZZ'],
            include_docs=True)
        return list(r)


    @classmethod
    def for_device(cls, device_id):
        r = PodcastUserState.view('users/podcast_states_by_device',
            startkey=[device_id, None], endkey=[device_id, {}],
            include_docs=True)
        return list(r)


    @classmethod
    def count(cls):
        r = PodcastUserState.view('users/podcast_states_by_user',
            limit=0)
        return r.total_rows


    def add_actions(self, actions):
        self.actions = list(set(self.actions + actions))
        self.actions = sorted(self.actions)


    def add_tags(self, tags):
        self.tags = list(set(self.tags + tags))


    def set_device_state(self, device):
        if device.deleted:
            self.disabled_devices = list(set(self.disabled_devices + [device.id]))
        elif not device.deleted and device.id in self.disabled_devices:
            self.disabled_devices.remove(device.id)


    def get_change_between(self, device_id, since, until):
        """
        Returns the change of the subscription status for the given device
        between the two timestamps.

        The change is given as either 'subscribe' (the podcast has been
        subscribed), 'unsubscribed' (the podcast has been unsubscribed) or
        None (no change)
        """

        device_actions = filter(lambda x: x.device == device_id, self.actions)
        before = filter(lambda x: x.timestamp <= since, device_actions)
        after  = filter(lambda x: x.timestamp <= until, device_actions)

        then = before[-1] if before else None
        now  = after[-1]

        if then is None:
            if now.action != 'unsubscribe':
                return now.action
        elif then.action != now.action:
            return now.action
        return None


    def __eq__(self, other):
        if other is None:
            return False

        return self.podcast == other.podcast and \
               self.user_oldid == other.user_oldid

    def __repr__(self):
        return 'Podcast %s for User %s (%s)' % \
            (self.podcast, self.user_oldid, self._id)


class Device(Document):
    id       = StringProperty(default=lambda: uuid.uuid4().hex)
    oldid    = IntegerProperty()
    uid      = StringProperty()
    name     = StringProperty()
    type     = StringProperty()
    settings = DictProperty()
    deleted  = BooleanProperty()

    @classmethod
    def for_oldid(cls, oldid):
        r = cls.view('users/devices_by_oldid', key=oldid)
        return r.first() if r else None


    def get_subscription_changes(self, since, until):
        """
        Returns the subscription changes for the device as two lists.
        The first lists contains the Ids of the podcasts that have been
        subscribed to, the second list of those that have been unsubscribed
        from.
        """

        add, rem = [], []
        podcast_states = PodcastUserState.for_device(self.id)
        for p_state in podcast_states:
            change = p_state.get_change_between(self.id, since, until)
            if change == 'subscribe':
                add.append( p_state.podcast )
            elif change == 'unsubscribe':
                rem.append( p_state.podcast )

        return add, rem


    def get_subscribed_podcasts_ids(self):
        r = self.view('users/subscribed_podcasts_by_device',
            startkey=[self.id, None],
            endkey=[self.id, {}])
        return [res['key'][1] for res in r]


    def get_subscribed_podcasts(self):
        return Podcast.get_mutlti(self.get_subscribed_podcasts_ids())


def token_generator(length=32):
    import random, string
    return  "".join(random.sample(string.letters+string.digits, length))


class User(Document):
    oldid    = IntegerProperty()
    settings = DictProperty()
    devices  = SchemaListProperty(Device)
    published_objects = StringListProperty()

    # token for accessing subscriptions of this use
    subscriptions_token    = StringProperty(default=token_generator)

    # token for accessing the favorite-episodes feed of this user
    favorite_feeds_token   = StringProperty(default=token_generator)

    # token for automatically updating feeds published by this user
    publisher_update_token = StringProperty(default=token_generator)


    @classmethod
    def for_oldid(cls, oldid):
        r = cls.view('users/users_by_oldid', key=oldid, limit=1, include_docs=True)
        return r.one() if r else None


    def create_new_token(self, token_name, length=32):
        setattr(self, token_name, token_generator(length))


    def get_device(self, id):
        for device in self.devices:
            if device.id == id:
                return device

        return None


    def set_device(self, device):
        devices = list(self.devices)
        ids = [x.id for x in devices]
        if not device.id in ids:
            devices.append(device)
            return

        index = ids.index(device.id)
        devices.pop(index)
        devices.insert(index, device)
        self.devices = devices


    def remove_device(self, device):
        devices = list(self.devices)
        ids = [x.id for x in devices]
        if not device.id in ids:
            return

        index = ids.index(device.id)
        devices.pop(index)
        self.devices = devices


    def get_subscriptions(self):
        """
        Returns a list of (podcast-id, device-id) tuples for all
        of the users subscriptions
        """

        r = PodcastUserState.view('users/subscribed_podcasts_by_user',
            startkey=[self.oldid, None, None],
            endkey=[self.oldid+1, None, None])
        return [res['key'][1:] for res in r]


    def get_subscribed_podcast_ids(self):
        """
        Returns the Ids of all subscribed podcasts
        """
        return list(set(x[0] for x in self.get_subscriptions()))


    def get_subscribed_podcasts(self):
        return Podcast.get_multi(self.get_subscribed_podcast_ids())


    def __repr__(self):
        return 'User %s' % self._id

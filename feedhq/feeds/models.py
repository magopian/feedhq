import datetime
import feedparser
import json
import logging
import lxml
import magic
import oauth2 as oauth
import urllib
import urlparse
import random
import requests
import socket

from django.db import models
from django.db.models import F
from django.conf import settings
from django.contrib.auth.models import User
from django.core.files.base import ContentFile
from django.core.urlresolvers import reverse
from django.utils import timezone
from django.utils.translation import ugettext_lazy as _

from django_push.subscriber.signals import updated

from .tasks import update_feed, update_unique_feed
from .utils import FeedUpdater, FAVICON_FETCHER, FEED_CHECKER, USER_AGENT
from ..storage import OverwritingStorage
from ..tasks import enqueue

logger = logging.getLogger('feedupdater')

COLORS = (
        ('red', _('Red')),
        ('dark-red', _('Dark Red')),
        ('pale-green', _('Pale Green')),
        ('green', _('Green')),
        ('army-green', _('Army Green')),
        ('pale-blue', _('Pale Blue')),
        ('blue', _('Blue')),
        ('dark-blue', _('Dark Blue')),
        ('orange', _('Orange')),
        ('dark-orange', _('Dark Orange')),
        ('black', _('Black')),
        ('gray', _('Gray')),
)


def random_color():
    return random.choice(COLORS)[0]


DURATIONS = (
        ('1day', _('One day')),
        ('2days', _('Two days')),
        ('1week', _('One week')),
        ('1month', _('One month')),
        ('1year', _('One year')),
)


TIMEDELTAS = {
    '1day': datetime.timedelta(days=1),
    '2days': datetime.timedelta(days=2),
    '1week': datetime.timedelta(weeks=1),
    '1month': datetime.timedelta(days=30),
    '1year': datetime.timedelta(days=365),
    #'never': None, # Implicit
}


class CategoryManager(models.Manager):

    def with_unread_counts(self):
        return self.values('id', 'name', 'slug', 'color').annotate(
            unread_count=models.Sum('feeds__unread_count'))


class Category(models.Model):
    """Used to sort our feeds"""
    name = models.CharField(_('Name'), max_length=50)
    slug = models.SlugField(_('Slug'), db_index=True)
    user = models.ForeignKey(User, verbose_name=_('User'),
                             related_name='categories')
    # Some day there will be drag'n'drop ordering
    order = models.PositiveIntegerField(blank=True, null=True)

    # Categories have nice cute colors
    color = models.CharField(_('Color'), max_length=50, choices=COLORS,
                             default=random_color)

    # We delete the old entries after a certain while
    delete_after = models.CharField(
        _('Delete after'), max_length=50, choices=DURATIONS, default='1month',
        help_text=_("Period of time after which entries are deleted, whether "
                    "they've been read or not."),
    )

    objects = CategoryManager()

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('order', 'name', 'id')
        verbose_name_plural = 'categories'

    def get_absolute_url(self):
        return reverse('feeds:category', args=[self.slug])


class UniqueFeedManager(models.Manager):
    def update_feed(self, url, use_etags=True):
        obj, created = self.get_or_create(url=url)
        save = True

        if not created and use_etags:
            if not obj.should_update():
                logger.debug("Last update too recent, skipping %s" % obj.url)
                return
        obj.last_update = timezone.now()

        if obj.muted:
            logger.debug("%s is muted" % obj.url)
            return

        feeds = Feed.objects.filter(url=url)

        obj.subscribers = feeds.count()

        if obj.subscribers == 1:
            subscribers = '1 subscriber'
        else:
            subscribers = '%s subscribers' % obj.subscribers

        headers = {
            'User-Agent': USER_AGENT % subscribers,
        }

        if use_etags:
            if obj.modified:
                headers['If-Modified-Since'] = obj.modified
            if obj.etag:
                headers['If-None-Match'] = obj.etag

        if settings.TESTS:
            # Make sure requests.get is properly mocked during tests
            if str(type(requests.get)) != "<class 'mock.MagicMock'>":
                raise ValueError("Not Mocked")

        try:
            response = requests.get(url, headers=headers, timeout=10)
        except (requests.RequestException, socket.timeout) as e:
            logger.debug("Error fetching %s, %s" % (obj.url, str(e)))
            obj.failed_attempts += 1
            if obj.failed_attempts >= 20:
                logger.info("%s failed 20 times, muting" % obj.url)
                obj.muted = True
                obj.muted_reason = 'timeout'
            if save:
                obj.save()
            return

        if (response.history and
            obj.url != response.url and 'Content-Type' in response.headers and
            response.headers['Content-Type'].startswith('application')):
            logger.info("%s moved to %s" % (obj.url, response.url))
            Feed.objects.filter(url=obj.url).update(url=response.url)
            if self.filter(url=response.url).exists():
                obj.delete()
                save = False
            else:
                obj.url = response.url

        if response.status_code == 410:
            logger.info("Feed gone, %s" % obj.url)
            obj.muted = True
            obj.muted_reason = 'gone'
            obj.save()
            return

        elif response.status_code in [400, 401, 403, 404, 500, 502, 503]:
            obj.failed_attempts += 1
            if obj.failed_attempts >= 5:
                obj.muted = True
                logger.info("%s returned %s, muting" % (
                    obj.url, response.status_code,
                ))
                obj.muted_reason = str(response.status_code)
            if save:
                obj.save()
            return

        elif response.status_code not in [200, 204, 304]:
            logger.debug("%s returned %s" % (obj.url, response.status_code))

        else:
            obj.failed_attempts = 0
            obj.muted_reason = None

        if 'etag' in response.headers:
            obj.etag = response.headers['etag']

        if 'last-modified' in response.headers:
            obj.modified = response.headers['last-modified']

        if response.status_code == 304:
            logger.debug("Feed not modified, %s" % obj.url)
            if save:
                obj.save()
            return

        try:
            if not response.content:
                content = ' '  # chardet won't detect encoding on empty strings
            else:
                content = response.content
        except socket.timeout:
            logger.debug('%s timed out' % obj.url)
            return
        parsed = feedparser.parse(content)

        if 'link' in parsed.feed:
            obj.link = parsed.feed.link

        if 'title' in parsed.feed:
            obj.title = parsed.feed.title

        if 'links' in parsed.feed:
            for link in parsed.feed.links:
                if link.rel == 'hub':
                    obj.hub = link.href

        if save:
            obj.save()

        updater = FeedUpdater(parsed=parsed, feeds=feeds, hub=obj.hub)
        updater.update()


MUTE_CHOICES = (
    ('gone', 'Feed gone (410)'),
    ('timeout', 'Feed timed out'),
    ('400', 'HTTP 400'),
    ('401', 'HTTP 401'),
    ('403', 'HTTP 403'),
    ('404', 'HTTP 404'),
    ('500', 'HTTP 500'),
    ('502', 'HTTP 502'),
    ('503', 'HTTP 503'),
)


class UniqueFeed(models.Model):
    url = models.URLField(_('URL'), verify_exists=False, max_length=1023,
                          unique=True)
    title = models.CharField(_('Title'), max_length=1023, blank=True)
    link = models.URLField(_('Link'), verify_exists=False, max_length=1023,
                           blank=True)
    etag = models.CharField(_('Etag'), max_length=1023, null=True, blank=True)
    modified = models.CharField(_('Modified'), max_length=1023, null=True,
                                blank=True)
    subscribers = models.PositiveIntegerField(default=1, db_index=True)
    last_update = models.DateTimeField(_('Last update'), default=timezone.now,
                                       db_index=True)
    muted = models.BooleanField(_('Muted'), default=False, db_index=True)
    muted_reason = models.CharField(_('Muting reason'), max_length=50,
                                    null=True, blank=True,
                                    choices=MUTE_CHOICES)
    failed_attempts = models.PositiveIntegerField(_('Failed fetch attempts'),
                                                  default=0)
    hub = models.URLField(_('Hub'), max_length=1023, null=True, blank=True)

    objects = UniqueFeedManager()

    def __unicode__(self):
        if self.title:
            return u'%s' % self.title
        return u'%s' % self.url

    def resurrect(self):
        if not self.muted:
            return
        ua = {'User-Agent': FEED_CHECKER}
        try:
            response = requests.head(self.url, headers=ua, timeout=20)
        except requests.exceptions.RequestException:
            logger.debug("Feed still dead, raised exception. %s" % self.url)
        else:
            if response.status_code == 200:
                logger.info("Unmuting %s" % self.url)
                self.muted = False
                self.muted_reason = None
                self.failed_attempts = 0
                self.save()
                return
            else:
                logger.debug("Feed still dead, status %s, %s" % (
                    response.status_code, self.url))
        UniqueFeed.objects.filter(pk=self.pk).update(
            failed_attempts=F('failed_attempts') + 1
        )

    def should_update(self):
        delay = datetime.timedelta(minutes=45)
        return self.last_update + delay < timezone.now()


class Feed(models.Model):
    """A URL and some extra stuff"""
    name = models.CharField(_('Name'), max_length=255)
    url = models.URLField(_('URL'), verify_exists=False, max_length=1023)
    category = models.ForeignKey(
        Category, verbose_name=_('Category'), related_name='feeds',
        help_text=_('<a href="/category/add/">Add a category</a>'),
    )
    # The next 2 are RSS/ATOM attributes
    title = models.CharField(_('Title'), max_length=255)
    link = models.URLField(_('Link'), verify_exists=False, max_length=1023)
    # Mute a feed when we don't want the updates to show up in the timeline
    muted = models.BooleanField(_('Muted'), default=False,
                                help_text=_('Check this if you want to stop '
                                           'checking updates for this feed'))
    etag = models.CharField(_('Etag'), max_length=1023, null=True, blank=True)
    modified = models.CharField(_('Modified'), max_length=255, null=True,
                                blank=True)
    unread_count = models.PositiveIntegerField(_('Unread count'), default=0)
    favicon = models.ImageField(_('Favicon'), upload_to='favicons', null=True,
                                storage=OverwritingStorage())
    no_favicon = models.BooleanField(_('No favicon'), default=False)
    img_safe = models.BooleanField(_('Display images by default'),
                                   default=False)
    failed_attempts = models.IntegerField(_('Failed fetching attempts'),
                                          default=0)

    def __unicode__(self):
        return u'%s' % self.name

    class Meta:
        ordering = ('name',)

    def get_absolute_url(self):
        return reverse('feeds:feed', args=[self.id])

    def save(self, *args, **kwargs):
        update = self.pk is None
        super(Feed, self).save(*args, **kwargs)
        if update:
            enqueue(update_feed, self.url, use_etags=False, timeout=20,
                    queue='high')
        enqueue(update_unique_feed, self.url, timeout=20)

    def favicon_img(self):
        if not self.favicon:
            return ''
        return '<img src="%s" width="16" height="16" />' % self.favicon.url
    favicon_img.allow_tags = True

    def get_treshold(self):
        """Returns the date after which the entries can be ignored / deleted"""
        del_after = self.category.delete_after

        if del_after == 'never':
            return None
        return timezone.now() - TIMEDELTAS[del_after]

    def update_unread_count(self):
        self.unread_count = self.entries.filter(read=False).count()
        Feed.objects.filter(pk=self.pk).update(
            unread_count=self.unread_count,
        )


class EntryManager(models.Manager):
    def unread(self):
        return self.filter(read=False).count()


class Entry(models.Model):
    """An entry is a cached feed item"""
    feed = models.ForeignKey(Feed, verbose_name=_('Feed'),
                             related_name='entries')
    title = models.CharField(_('Title'), max_length=255)
    subtitle = models.TextField(_('Abstract'))
    link = models.URLField(_('URL'), verify_exists=False, max_length=1023)
    # We also have a permalink for feed proxies (like FeedBurner). If the link
    # points to feedburner, the redirection (=real feed link) is put here
    permalink = models.URLField(_('Permalink'), verify_exists=False,
                                max_length=1023, blank=True)
    date = models.DateTimeField(_('Date'), db_index=True)
    # The User FK is redundant but this may be better for performance and if
    # want to allow user input.
    user = models.ForeignKey(User, verbose_name=(_('User')),
                             related_name='entries')
    # Mark something as read or unread
    read = models.BooleanField(_('Read'), default=False, db_index=True)
    # Read later: store the URL
    read_later_url = models.URLField(_('Read later URL'), verify_exists=False,
                                 max_length=1023, blank=True)

    objects = EntryManager()

    def __unicode__(self):
        return u'%s' % self.title

    class Meta:
        # Display most recent entries first
        ordering = ('-date', 'title')
        verbose_name_plural = 'entries'

    def get_absolute_url(self):
        return reverse('feeds:item', args=[self.id])

    def get_link(self):
        if self.permalink:
            return self.permalink
        return self.link

    def link_domain(self):
        return urlparse.urlparse(self.get_link()).netloc

    def read_later_domain(self):
        netloc = urlparse.urlparse(self.read_later_url).netloc
        return netloc.replace('www.', '')

    def read_later(self):
        """Adds this item to the user's read list"""
        user = self.user
        if not user.read_later:
            return
        getattr(self, 'add_to_%s' % self.user.read_later)()

    def add_to_readitlater(self):
        url = 'https://readitlaterlist.com/v2/add'
        data = json.loads(self.user.read_later_credentials)
        data.update({
            'apikey': settings.API_KEYS['readitlater'],
            'url': self.get_link(),
            'title': self.title,
        })
        # The readitlater API doesn't return anything back
        requests.post(url, data=data)

    def add_to_readability(self):
        url = 'https://www.readability.com/api/rest/v1/bookmarks'
        client = self.oauth_client('readability')
        params = {'url': self.get_link()}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        response, data = client.request(response['location'], method='GET')
        url = 'https://www.readability.com/articles/%s'
        self.read_later_url = url % json.loads(data)['article']['id']
        self.save()

    def add_to_instapaper(self):
        url = 'https://www.instapaper.com/api/1/bookmarks/add'
        client = self.oauth_client('instapaper')
        params = {'url': self.get_link()}
        response, data = client.request(url, method='POST',
                                        body=urllib.urlencode(params))
        url = 'https://www.instapaper.com/read/%s'
        url = url % json.loads(data)[0]['bookmark_id']
        self.read_later_url = url
        self.save()

    def oauth_client(self, service):
        service_settings = getattr(settings, service.upper())
        consumer = oauth.Consumer(service_settings['CONSUMER_KEY'],
                                  service_settings['CONSUMER_SECRET'])
        creds = json.loads(self.user.read_later_credentials)
        token = oauth.Token(key=creds['oauth_token'],
                            secret=creds['oauth_token_secret'])
        client = oauth.Client(consumer, token)
        client.set_signature_method(oauth.SignatureMethod_HMAC_SHA1())
        return client


def pubsubhubbub_update(notification, **kwargs):
    parsed = notification
    url = None
    for link in parsed.feed.links:
        if link['rel'] == 'self':
            url = link['href']
    if url is None:
        return
    feeds = Feed.objects.filter(url=url)
    updater = FeedUpdater(parsed, feeds)
    updater.update()
updated.connect(pubsubhubbub_update)


class FaviconManager(models.Manager):
    def update_favicon(self, link, force_update=False):
        if not link:
            return
        parsed = list(urlparse.urlparse(link))
        if not parsed[0].startswith('http'):
            return
        favicon, created = self.get_or_create(url=link)
        urls = UniqueFeed.objects.filter(link=link).values_list('url',
                                                                flat=True)
        feeds = Feed.objects.filter(url__in=urls, favicon='')
        if not created and not force_update:
            # Still, add to existing
            favicon_url = self.filter(url=link).values_list('favicon',
                                                            flat=True)[0]
            if not favicon_url:
                return favicon

            if not feeds.exists():
                return

            feeds.update(favicon=favicon_url)
            return favicon

        ua = {'User-Agent': FAVICON_FETCHER}

        try:
            page = requests.get(link, headers=ua, timeout=10).content
        except requests.RequestException:
            return favicon
        if not page:
            return favicon

        icon_path = lxml.html.fromstring(page.lower()).xpath(
            '//link[@rel="icon" or @rel="shortcut icon"]/@href'
        )

        if not icon_path:
            parsed[2] = '/favicon.ico'  # 'path' element
            icon_path = [urlparse.urlunparse(parsed)]
        if not icon_path[0].startswith('http'):
            parsed[2] = icon_path[0]
            parsed[3] = parsed[4] = parsed[5] = ''
            icon_path = [urlparse.urlunparse(parsed)]
        try:
            response = requests.get(icon_path[0], headers=ua, timeout=10)
        except requests.RequestException:
            return favicon
        if response.status_code != 200:
            return favicon

        icon_file = ContentFile(response.content)
        m = magic.Magic()
        icon_type = m.from_buffer(response.content)
        if 'PNG' in icon_type:
            ext = 'png'
        elif 'MS Windows icon' in icon_type:
            ext = 'ico'
        elif 'GIF' in icon_type:
            ext = 'gif'
        elif 'JPEG' in icon_type:
            ext = 'jpg'
        elif 'PC bitmap' in icon_type:
            ext = 'bmp'
        elif icon_type == 'data':
            ext = 'ico'
        elif ('HTML' in icon_type or
              icon_type == 'empty' or
              'Photoshop' in icon_type or
              'ASCII' in icon_type):
            logger.debug("Ignored content type for %s: %s" % (link, icon_type))
            return favicon
        else:
            logger.info("Unknown content type for %s: %s" % (link, icon_type))
            favicon.delete()
            return

        filename = '%s.%s' % (urlparse.urlparse(favicon.url).netloc, ext)
        favicon.favicon.save(filename, icon_file)

        for feed in feeds:
            feed.favicon.save(filename, icon_file)
        feeds.update(no_favicon=False)
        return favicon


class Favicon(models.Model):
    url = models.URLField(_('Domain URL'), db_index=True)
    favicon = models.FileField(upload_to='favicons', blank=True,
                               storage=OverwritingStorage())

    objects = FaviconManager()

    def __unicode__(self):
        return u'Favicon for %s' % self.url

    def favicon_img(self):
        if not self.favicon:
            return '(None)'
        return '<img src="%s">' % self.favicon.url
    favicon_img.allow_tags = True

import datetime
import math
import os
import sys
import time
import urllib.parse
from typing import Any, Dict, List, Set, Tuple, Union

import xbmc
import xbmcaddon
import xbmcgui
import xbmcplugin
import xbmcvfs

import main_service
import simplecache
import spotipy
import spotty
import utils
from spotipy import SpotifyException
from spotty_auth import SpottyAuth
from spotty_helper import SpottyHelper
from string_ids import *
from utils import ADDON_ID, PROXY_PORT, log_exception, log_msg, get_chunks

MUSIC_ARTISTS_ICON = "icon_music_artists.png"
MUSIC_TOP_ARTISTS_ICON = "icon_music_top_artists.png"
MUSIC_SONGS_ICON = "icon_music_songs.png"
MUSIC_TOP_TRACKS_ICON = "icon_music_top_tracks.png"
MUSIC_ALBUMS_ICON = "icon_music_albums.png"
MUSIC_PLAYLISTS_ICON = "icon_music_playlists.png"
MUSIC_LIBRARY_ICON = "icon_music_library.png"
MUSIC_SEARCH_ICON = "icon_music_search.png"
MUSIC_EXPLORE_ICON = "icon_music_explore.png"
CLEAR_CACHE_ICON = "icon_clear_cache.png"

Playlist = Dict[str, Union[str, Dict[str, List[Any]]]]

DO_CACHE_LOGGING = False

# How long the user's library (saved tracks, saved albums, followed artists) is kept
# before asking Spotify again. Likes and follows done in Kodi call refresh_listing(),
# which changes the library checksum, so those show up at once.
LIBRARY_CACHE_TIME = datetime.timedelta(minutes=10)

# How long we remember who the user is (id, email, country).
ME_CACHE_TIME = datetime.timedelta(days=1)

# Pause between precache steps. It gives the user's own clicks the CPU first.
PRECACHE_PAUSE_SECONDS = 0.2

# Window property that changes after a like, save or follow done in Kodi.
LIBRARY_VERSION_PROPERTY = "spotify.library.version"


def cache_log(msg) -> None:
    if DO_CACHE_LOGGING:
        log_msg(msg)


def _get_len(items) -> int:
    if not items:
        return 0
    return len(items)


class PluginContent:
    __addon: xbmcaddon.Addon = xbmcaddon.Addon(id=ADDON_ID)
    __win: xbmcgui.Window = xbmcgui.Window(utils.ADDON_WINDOW_ID)
    __addon_icon_path = os.path.join(__addon.getAddonInfo("path"), "resources")
    __action = ""
    __spotty: spotty.Spotty = None
    __spotipy: spotipy.Spotify = None
    __userid = ""
    __username = ""
    __user_country = ""
    __offset = 0
    __playlist_id = ""
    __album_id = ""
    __track_id = ""
    __artist_id = ""
    __artist_name = ""
    __owner_id = ""
    __filter = ""
    __token = ""
    __limit = 50
    __params = {}
    __base_url = sys.argv[0]
    __addon_handle = int(sys.argv[1])
    __last_playlist_position = 0

    def __init__(self):
        try:
            # logging.basicConfig(level=logging.DEBUG)

            self.cache: simplecache.SimpleCache = simplecache.SimpleCache(ADDON_ID)

            self.append_artist_to_title: bool = (
                self.__addon.getSetting("appendArtistToTitle") == "true"
            )
            self.default_view_songs: str = self.__addon.getSetting("songDefaultView")
            self.default_view_artists: str = self.__addon.getSetting("artistDefaultView")
            self.default_view_playlists: str = self.__addon.getSetting("playlistDefaultView")
            self.default_view_albums: str = self.__addon.getSetting("albumDefaultView")
            self.default_view_category: str = self.__addon.getSetting("categoryDefaultView")

            self.__spotty: spotty.Spotty = spotty.get_spotty(SpottyHelper())

            self.check_auth_and_refresh_spotipy()

            self.parse_params()

            if self.__action:
                log_msg(f"Evaluating action '{self.__action}'.")
                action = f"self.{self.__action}"
                eval(action)()
            else:
                log_msg("Browsing main and setting up precache library.")
                self.__browse_main()
                self.__precache_library()

        except Exception as exc:
            log_exception(exc, "PluginContent init error")
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def check_auth_and_refresh_spotipy(self):
        auth_token: str = utils.get_cached_auth_token()
        if auth_token:
            log_msg(f"Got cached auth_token '{auth_token}'.")
            try_authenticate = False
            try:
                self.init_spotipy(auth_token)
            except SpotifyException as exc:
                log_exception(exc, f"HTTPError init_spotipy error: http_status = {exc.http_status}")
                if exc.http_status == 401:
                    try_authenticate = True
            except Exception as exc:
                log_exception(exc, "PluginContent init_spotipy error")
            if not try_authenticate:
                return

        log_msg(f"Cold not get valid cached auth_token. Attempting to authenticate.")
        self.authenticate_plugin_after_login_failure()

    def refresh_spotipy(self):
        auth_token: str = utils.get_cached_auth_token()
        if not auth_token:
            xbmcplugin.endOfDirectory(handle=self.__addon_handle)
            return

        log_msg(f"Got auth_token '{auth_token}'.")

        self.init_spotipy(auth_token)


    def init_spotipy(self, auth_token: str) -> None:
        self.__spotipy: spotipy.Spotify = spotipy.Spotify(auth=auth_token)
        me = self.__get_me()
        self.__userid: str = me["id"]
        self.__username: str = me["email"]
        self.__user_country = me["country"]

    def authenticate_plugin_after_login_failure(self) -> None:
        self.authenticate_plugin(
            self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_AFTER_LOGIN_FAIL_STR_ID)
        )

    def authenticate_plugin_request(self) -> None:
        self.authenticate_plugin(self.__addon.getLocalizedString(AUTHENTICATE_INSTRUCTIONS_STR_ID))

    def authenticate_plugin(self, instructions: str) -> None:
        dialog = xbmcgui.Dialog()
        dialog_title = self.__addon.getAddonInfo("name")

        client_id = self.__addon.getSetting("user_client_id")
        spotty_auth = SpottyAuth(self.__spotty, client_id)

        zeroconf_auth = spotty_auth.start_zeroconf_authenticate()
        if zeroconf_auth is None:
            dialog.ok(dialog_title, self.get_zeroconf_program_failed_msg(spotty_auth))
            main_service.abort_main_service = True
            utils.kill_this_plugin()
            return

        dialog.ok(dialog_title, instructions)

        zeroconf_auth.terminate()

        if not spotty_auth.zeroconf_authenticated_ok():
            dialog.ok(dialog_title, self.get_zeroconf_authentication_failed_msg(spotty_auth))
            main_service.abort_main_service = True
            utils.kill_this_plugin()
            return

        spotty_auth.renew_token()
        self.refresh_spotipy()

        dialog.ok(dialog_title, self.get_authenticated_success_msg())

    def get_authenticated_success_msg(self) -> str:
        msg = self.__addon.getLocalizedString(AUTHENTICATE_SUCCESS_STR_ID)

        max_str_len = len(max(msg.split("\n"), key=len))
        blanks = " " * (int(max_str_len / 2) - 1)
        msg += f"\n\n{blanks}'{self.__username}'."

        return msg

    def get_zeroconf_program_failed_msg(self, spotty_auth: SpottyAuth) -> str:
        return (
            f"{spotty_auth.get_zeroconf_program_failed_msg()}\n\n"
            f"{self.__addon.getLocalizedString(TERMINATING_SPOTIFY_PLUGIN_STR_ID)}"
        )

    def get_zeroconf_authentication_failed_msg(self, spotty_auth: SpottyAuth) -> str:
        return (
            f"{spotty_auth.get_zeroconf_authentication_failed_msg()}\n\n"
            f"{self.__addon.getLocalizedString(TERMINATING_SPOTIFY_PLUGIN_STR_ID)}"
        )

    def parse_params(self):
        """parse parameters from the plugin entry path"""
        log_msg(f"sys.argv = {str(sys.argv)}")
        self.__params: Dict[str, Any] = urllib.parse.parse_qs(sys.argv[2][1:])

        action = self.__params.get("action", None)
        if action:
            self.__action = action[0].lower()
            log_msg(f"Set action to '{self.__action}'.")

        playlist_id = self.__params.get("playlistid", None)
        if playlist_id:
            self.__playlist_id = playlist_id[0]
        owner_id = self.__params.get("ownerid", None)
        if owner_id:
            self.__owner_id = owner_id[0]
        track_id = self.__params.get("trackid", None)
        if track_id:
            self.__track_id = track_id[0]
        album_id = self.__params.get("albumid", None)
        if album_id:
            self.__album_id = album_id[0]
        artist_id = self.__params.get("artistid", None)
        if artist_id:
            self.__artist_id = artist_id[0]
        artist_name = self.__params.get("artistname", None)
        if artist_name:
            self.__artist_name = artist_name[0]
        offset = self.__params.get("offset", None)
        if offset:
            self.__offset = int(offset[0])
        filt = self.__params.get("applyfilter", None)
        if filt:
            self.__filter = filt[0]

    def __cache_checksum(self, opt_value: Any = None) -> str:
        """Checksum for cached Spotify data. It does not depend on the user's library,
        because context menus are built when a list is shown, not when it is cached."""
        result = f"data-{self.__addon.getSetting('cache_checksum')}"
        if opt_value:
            result += f"-{opt_value}"

        return result

    def __build_url(self, query: Dict[str, str]) -> str:
        query_encoded = {}
        for key, value in list(query.items()):
            if isinstance(key, str):
                key = key.encode("utf-8")
            if isinstance(value, str):
                value = value.encode("utf-8")
            query_encoded[key] = value

        return self.__base_url + "?" + urllib.parse.urlencode(query_encoded)

    def delete_cache_db(self) -> None:
        log_msg("Deleting plugin cache...")
        simple_db_cache_addon = xbmcaddon.Addon(ADDON_ID)
        db_path = simple_db_cache_addon.getAddonInfo("profile")
        db_file = xbmcvfs.translatePath(f"{db_path}/simplecache.db")
        os.remove(db_file)
        log_msg(f"Deleted simplecache database file {db_file}.")

        dialog = xbmcgui.Dialog()
        header = self.__addon.getAddonInfo("name")
        msg = self.__addon.getLocalizedString(CACHED_CLEARED_STR_ID)
        dialog.ok(header, msg)

    def __get_me(self) -> Dict[str, str]:
        # Who the user is almost never changes, so do not ask Spotify on every click.
        # A different client_id can mean a different login, so it is part of the checksum.
        cache_str = "spotify.me"
        checksum = f"me-{self.__addon.getSetting('user_client_id')}"
        me = self.cache.get(cache_str, checksum=checksum)
        if me and self.__auth_token_is_fresh():
            return me

        result = self.__spotipy.me()
        me = {"id": result["id"], "email": result["email"], "country": result["country"]}
        self.cache.set(cache_str, me, checksum=checksum, expiration=ME_CACHE_TIME)
        return me

    def __auth_token_is_fresh(self) -> bool:
        # The me() call used to double as a token check (a bad token gives a 401 and
        # a new login). Without that call, check the expiry time the service stored.
        expires_at = self.__win.getProperty(utils.KODI_PROPERTY_AUTH_TOKEN_EXPIRES_AT)
        try:
            return int(expires_at) > int(time.time()) + 30
        except ValueError:
            return False

    def __library_checksum(self) -> str:
        # Never empty: an empty checksum would make simplecache skip the check and
        # return old entries written by earlier versions.
        version = xbmcgui.Window(10000).getProperty(LIBRARY_VERSION_PROPERTY)
        return f"library-{self.__addon.getSetting('cache_checksum')}-{version}"

    def __library_changed(self) -> None:
        # Only the small library lists must be reloaded, not the whole cache.
        xbmcgui.Window(10000).setProperty(LIBRARY_VERSION_PROPERTY, str(time.time()))
        xbmc.executebuiltin("Container.Refresh")

    def refresh_listing(self) -> None:
        self.__addon.setSetting("cache_checksum", time.strftime("%Y%m%d%H%M%S", time.gmtime()))
        log_msg(f"New cache_checksum = '{self.__addon.getSetting('cache_checksum')}'")
        xbmc.executebuiltin("Container.Refresh")

    def __add_track_listitems(self, tracks, append_artist_to_label: bool = False) -> None:
        list_items = self.__get_track_list(tracks, append_artist_to_label)
        xbmcplugin.addDirectoryItems(self.__addon_handle, list_items, totalItems=len(list_items))

    @staticmethod
    def __get_track_name(track, append_artist_to_label: bool) -> str:
        if not append_artist_to_label:
            return track["name"]
        return f"{track['artist']} - {track['name']}"

    @staticmethod
    def __get_track_rating(popularity: int) -> int:
        if not popularity:
            return 0

        return int(math.ceil(popularity * 6 / 100.0)) - 1

    def __get_track_list(
        self, tracks, append_artist_to_label: bool = False
    ) -> List[Tuple[str, xbmcgui.ListItem, bool]]:
        saved_track_ids = set(self.__get_saved_track_ids())
        followed_artist_ids = self.__get_followed_artist_ids()

        list_items = []
        for track in tracks:
            context_items = self.__get_playlist_track_context_menu_items(
                track, saved_track_ids, followed_artist_ids
            )
            list_items.append(
                self.__get_track_item(track, append_artist_to_label, context_items) + (False,)
            )

        return list_items

    def __get_track_item(
        self,
        track: Dict[str, Any],
        append_artist_to_label: bool = False,
        context_items: List[Tuple[str, str]] = None,
    ) -> Tuple[str, xbmcgui.ListItem]:
        duration = track["duration_ms"] / 1000
        label = self.__get_track_name(track, append_artist_to_label)
        title = label if self.append_artist_to_title else track["name"]

        # Local playback by using proxy on this machine.
        url = f"http://localhost:{PROXY_PORT}/track/{track['id']}/{duration}"

        li = xbmcgui.ListItem(label, offscreen=True)
        li.setProperty("isPlayable", "true")
        li.setInfo(
            "music",
            {
                "title": title,
                "genre": track["genre"],
                "year": track["year"],
                "tracknumber": track["track_number"],
                "album": track["album"]["name"],
                "artist": track["artist"],
                "rating": track["rating"],
                "duration": duration,
            },
        )
        li.setArt({"thumb": track["thumb"]})
        li.setProperty("spotifytrackid", track["id"])
        li.setContentLookup(False)
        if context_items:
            li.addContextMenuItems(context_items, True)
        li.setProperty("do_not_analyze", "true")
        li.setMimeType("audio/wave")

        return url, li

    def __browse_main(self) -> None:
        # Main listing.
        xbmcplugin.setContent(self.__addon_handle, "files")

        items = [
            (
                self.__addon.getLocalizedString(MY_MUSIC_FOLDER_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_main_library.__name__}",
                MUSIC_LIBRARY_ICON,
                True,
            ),
            (
                self.__addon.getLocalizedString(EXPLORE_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_main_explore.__name__}",
                MUSIC_EXPLORE_ICON,
                True,
            ),
            (
                xbmc.getLocalizedString(KODI_SEARCH_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.search.__name__}",
                MUSIC_SEARCH_ICON,
                True,
            ),
            (
                self.__addon.getLocalizedString(AUTHENTICATE_PLUGIN_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.authenticate_plugin_request.__name__}",
                CLEAR_CACHE_ICON,
                False,
            ),
            (
                self.__addon.getLocalizedString(CLEAR_CACHE_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.delete_cache_db.__name__}",
                CLEAR_CACHE_ICON,
                False,
            ),
        ]

        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=item[3]
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        log_msg("Finished setting up main menu.")

    def browse_main_library(self) -> None:
        # Library nodes.
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(MY_MUSIC_FOLDER_STR_ID),
        )

        items = [
            (
                xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID),
                f"plugin://{ADDON_ID}/"
                f"?action={self.browse_playlists.__name__}&ownerid={self.__userid}",
                MUSIC_PLAYLISTS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_ALBUMS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_albums.__name__}",
                MUSIC_ALBUMS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_SONGS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_tracks.__name__}",
                MUSIC_SONGS_ICON,
            ),
            (
                xbmc.getLocalizedString(KODI_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_saved_artists.__name__}",
                MUSIC_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(FOLLOWED_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_followed_artists.__name__}",
                MUSIC_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(MOST_PLAYED_ARTISTS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_top_artists.__name__}",
                MUSIC_TOP_ARTISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(MOST_PLAYED_TRACKS_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_top_tracks.__name__}",
                MUSIC_TOP_TRACKS_ICON,
            ),
        ]

        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def browse_top_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        result = self.__spotipy.current_user_top_artists(limit=20, offset=0)

        cache_str = f"spotify.topartists.{self.__userid}"
        checksum = self.__cache_checksum(result["total"])
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached top artists for user "{self.__userid}".')
        else:
            count = len(result["items"])
            while result["total"] > count:
                result["items"] += self.__spotipy.current_user_top_artists(limit=20, offset=count)[
                    "items"
                ]
                count += 50
            artists = self.__prepare_artist_listitems(result["items"])
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED top artists for user "{self.__userid}".'
            )
        self.__add_artist_listitems(artists)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_artists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_artists})")

    def browse_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        results = self.__spotipy.current_user_top_tracks(limit=20, offset=0)

        cache_str = f"spotify.toptracks.{self.__userid}"
        checksum = self.__cache_checksum(results["total"])
        tracks = self.cache.get(cache_str, checksum=checksum)
        if tracks:
            cache_log(f'Retrieved {len(tracks)} cached top tracks for user "{self.__userid}".')
        else:
            tracks = results["items"]
            while results["next"]:
                results = self.__spotipy.next(results)
                tracks.extend(results["items"])
            tracks = self.__prepare_track_listitems(tracks=tracks)
            self.cache.set(cache_str, tracks, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(tracks)} UNCACHED top tracks for user "{self.__userid}".'
            )
        self.__add_track_listitems(tracks, True)

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def __get_explore_categories(self) -> List[Tuple[Any, str, Union[str, Any]]]:
        items = []

        categories = self.__spotipy.categories(
            country=self.__user_country, limit=50, locale=self.__user_country
        )
        count = len(categories["categories"]["items"])
        while categories["categories"]["total"] > count:
            categories["categories"]["items"] += self.__spotipy.categories(
                country=self.__user_country, limit=50, offset=count, locale=self.__user_country
            )["categories"]["items"]
            count += 50

        for item in categories["categories"]["items"]:
            thumb = "DefaultMusicGenre.png"
            for icon in item["icons"]:
                thumb = icon["url"]
                break
            items.append(
                (
                    item["name"],
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.browse_category.__name__}&applyfilter={item['id']}",
                    thumb,
                )
            )

        return items

    def browse_main_explore(self) -> None:
        # Explore nodes.
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", self.__addon.getLocalizedString(EXPLORE_STR_ID)
        )
        items = [
            (
                self.__addon.getLocalizedString(FEATURED_PLAYLISTS_STR_ID),
                f"plugin://{ADDON_ID}/"
                f"?action={self.browse_playlists.__name__}&applyfilter=featured",
                MUSIC_PLAYLISTS_ICON,
            ),
            (
                self.__addon.getLocalizedString(ALL_NEW_RELEASES_STR_ID),
                f"plugin://{ADDON_ID}/?action={self.browse_new_releases.__name__}",
                MUSIC_ALBUMS_ICON,
            ),
        ]

        # Add categories.
        items += self.__get_explore_categories()
        for item in items:
            li = xbmcgui.ListItem(item[0], path=item[1])
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setArt({"icon": os.path.join(self.__addon_icon_path, item[2])})
            li.addContextMenuItems([], True)
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
            )

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __get_album_tracks(self, album: Dict[str, Any]) -> List[Dict[str, Any]]:
        cache_str = f"spotify.albumtracks{album['id']}"
        checksum = self.__cache_checksum()

        album_tracks = self.cache.get(cache_str, checksum=checksum)
        if album_tracks:
            cache_log(
                f'Retrieved {album["tracks"]["total"]} cached tracks for album "{album["name"]}".'
            )
        else:
            track_ids = []
            count = 0
            while album["tracks"]["total"] > count:
                tracks = self.__spotipy.album_tracks(
                    album["id"], market=self.__user_country, limit=50, offset=count
                )["items"]
                for track in tracks:
                    track_ids.append(track["id"])
                count += 50
            album_tracks = self.__prepare_track_listitems(track_ids, album_details=album)
            self.cache.set(cache_str, album_tracks, checksum=checksum)
            cache_log(
                f'Retrieved {album["tracks"]["total"]} UNCACHED tracks for album "{album["name"]}".'
            )

        return album_tracks

    def browse_album(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        album = self.__spotipy.album(self.__album_id, market=self.__user_country)
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", album["name"])
        tracks = self.__get_album_tracks(album)
        if album.get("album_type") == "compilation":
            self.__add_track_listitems(tracks, True)
        else:
            self.__add_track_listitems(tracks)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TRACKNUM)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ARTIST)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def artist_top_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
        )
        tracks = self.__spotipy.artist_top_tracks(self.__artist_id, country=self.__user_country)
        tracks = self.__prepare_track_listitems(tracks=tracks["tracks"])
        self.__add_track_listitems(tracks)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TRACKNUM)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def related_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
        )
        cache_str = f"spotify.relatedartists.{self.__artist_id}"
        checksum = self.__cache_checksum()
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached related artists for "{self.__artist_id}".')
        else:
            artists = self.__spotipy.artist_related_artists(self.__artist_id)
            artists = self.__prepare_artist_listitems(artists["artists"])
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED related artists for "{self.__artist_id}".'
            )
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_artists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_artists})")

    def __get_playlist_details(
        self, playlist_id: str, known_playlist: Dict[str, Any] = None
    ) -> Playlist:
        if (
            known_playlist
            and known_playlist.get("snapshot_id")
            and "total" in known_playlist.get("tracks", {})
        ):
            # The user's playlist list already has all we need. This saves one call.
            playlist = {
                "id": known_playlist["id"],
                "name": known_playlist["name"],
                "owner": {"id": known_playlist["owner"]["id"]},
                "snapshot_id": known_playlist["snapshot_id"],
                "tracks": {"total": known_playlist["tracks"]["total"]},
            }
        else:
            playlist = self.__spotipy.playlist(
                playlist_id,
                fields="tracks(total),name,owner(id),id,snapshot_id",
                market=self.__user_country,
            )
        # Get from cache first. Spotify changes 'snapshot_id' on every change to the
        # playlist, also when tracks are replaced or reordered and the total stays the same.
        cache_str = f"spotify.playlistdetails.{playlist['id']}"
        checksum = self.__cache_checksum(
            f"{playlist['tracks']['total']}-{playlist.get('snapshot_id', '')}"
        )
        # log_msg(f"Playlist cache_str = '{cache_str}', checksum = '{checksum}'.")
        playlist_details = self.cache.get(cache_str, checksum=checksum)
        if playlist_details:
            cache_log(
                f'Retrieved {playlist["tracks"]["total"]} cached playlist details'
                f' for "{playlist["name"]}".'
            )
        else:
            # Get listing from api.
            count = 0
            playlist_details = playlist
            playlist_details["tracks"]["items"] = []
            while playlist["tracks"]["total"] > count:
                playlist_details["tracks"]["items"] += self.__spotipy.playlist_items(
                    playlist["id"],
                    market=self.__user_country,
                    fields="",
                    limit=50,
                    offset=count,
                )["items"]
                count += 50
            playlist_details["tracks"]["items"] = self.__prepare_track_listitems(
                tracks=playlist_details["tracks"]["items"], playlist_details=playlist
            )
            # log_msg(f"playlist_details = {playlist_details}")
            self.cache.set(cache_str, playlist_details, checksum=checksum)
            # log_msg(f"Got new playlist - checksum = '{checksum}'")
            cache_log(
                f'Retrieved {playlist["tracks"]["total"]} UNCACHED playlist details'
                f' for "{playlist["name"]}".'
            )

        return playlist_details

    def browse_playlist(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        playlist_details = self.__get_playlist_details(self.__playlist_id)
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlist_details["name"])
        self.__add_track_listitems(playlist_details["tracks"]["items"], True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def play_playlist(self) -> None:
        """play entire playlist"""
        playlist_details = self.__get_playlist_details(self.__playlist_id)
        log_msg(f"Start playing playlist '{playlist_details['name']}'.")

        kodi_playlist = xbmc.PlayList(0)
        kodi_playlist.clear()

        def add_to_playlist(trk) -> None:
            url, li = self.__get_track_item(trk, True)
            kodi_playlist.add(url, li)

        # Add first track and start playing.
        add_to_playlist(playlist_details["tracks"]["items"][0])
        kodi_player = xbmc.Player()
        kodi_player.play(kodi_playlist)

        # Add remaining tracks to the playlist while already playing.
        for track in playlist_details["tracks"]["items"][1:]:
            add_to_playlist(track)

    def __get_category(self, categoryid: str) -> Playlist:
        category = self.__spotipy.category(
            categoryid, country=self.__user_country, locale=self.__user_country
        )
        playlists = self.__spotipy.category_playlists(
            categoryid, country=self.__user_country, limit=50, offset=0
        )
        playlists["category"] = category["name"]
        count = len(playlists["playlists"]["items"])
        while playlists["playlists"]["total"] > count:
            playlists["playlists"]["items"] += self.__spotipy.category_playlists(
                categoryid, country=self.__user_country, limit=50, offset=count
            )["playlists"]["items"]
            count += 50
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"]
        )

        return playlists

    def browse_category(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        playlists = self.__get_category(self.__filter)
        self.__add_playlist_listitems(playlists["playlists"]["items"])
        xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlists["category"])
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_category:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_category})")

    def follow_playlist(self) -> None:
        self.__spotipy.current_user_follow_playlist(self.__playlist_id)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def add_track_to_playlist(self) -> None:
        xbmc.executebuiltin("ActivateWindow(busydialog)")

        if not self.__track_id and xbmc.getInfoLabel("MusicPlayer.(1).Property(spotifytrackid)"):
            self.__track_id = xbmc.getInfoLabel("MusicPlayer.(1).Property(spotifytrackid)")

        own_playlists, own_playlist_names = utils.get_user_playlists(self.__spotipy, 50)
        own_playlist_names.append(xbmc.getLocalizedString(KODI_NEW_PLAYLIST_STR_ID))

        xbmc.executebuiltin("Dialog.Close(busydialog)")
        select = xbmcgui.Dialog().select(
            xbmc.getLocalizedString(KODI_SELECT_PLAYLIST_STR_ID), own_playlist_names
        )
        if select != -1 and own_playlist_names[select] == xbmc.getLocalizedString(
            KODI_NEW_PLAYLIST_STR_ID
        ):
            # create new playlist...
            kb = xbmc.Keyboard("", xbmc.getLocalizedString(KODI_ENTER_NEW_PLAYLIST_STR_ID))
            kb.setHiddenInput(False)
            kb.doModal()
            if kb.isConfirmed():
                name = kb.getText()
                playlist = self.__spotipy.user_playlist_create(self.__userid, name, False)
                self.__spotipy.playlist_add_items(playlist["id"], [self.__track_id])
        elif select != -1:
            playlist = own_playlists[select]
            self.__spotipy.playlist_add_items(playlist["id"], [self.__track_id])

    def remove_track_from_playlist(self) -> None:
        self.__spotipy.playlist_remove_all_occurrences_of_items(
            self.__playlist_id, [self.__track_id]
        )
        self.__library_changed()

    def unfollow_playlist(self) -> None:
        self.__spotipy.current_user_unfollow_playlist(self.__playlist_id)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def follow_artist(self) -> None:
        self.__spotipy.user_follow_artists([self.__artist_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def unfollow_artist(self) -> None:
        self.__spotipy.user_unfollow_artists([self.__artist_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def save_album(self) -> None:
        self.__spotipy.current_user_saved_albums_add([self.__album_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def remove_album(self) -> None:
        self.__spotipy.current_user_saved_albums_delete([self.__album_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def save_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_add([self.__track_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def remove_track(self) -> None:
        self.__spotipy.current_user_saved_tracks_delete([self.__track_id])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        self.__library_changed()

    def __get_featured_playlists(self) -> Playlist:
        playlists = self.__spotipy.featured_playlists(
            country=self.__user_country, limit=50, offset=0
        )
        count = len(playlists["playlists"]["items"])
        total = playlists["playlists"]["total"]
        while total > count:
            playlists["playlists"]["items"] += self.__spotipy.featured_playlists(
                country=self.__user_country, limit=50, offset=count
            )["playlists"]["items"]
            count += 50
        playlists["playlists"]["items"] = self.__prepare_playlist_listitems(
            playlists["playlists"]["items"]
        )

        return playlists

    def __get_user_playlists(self, userid):
        playlists = self.__spotipy.user_playlists(userid, limit=1, offset=0)
        count = len(playlists["items"])
        total = playlists["total"]
        cache_str = f"spotify.userplaylists.{userid}"
        checksum = self.__cache_checksum(total)

        cached_playlists = self.cache.get(cache_str, checksum=checksum)
        if cached_playlists:
            playlists = cached_playlists
            cache_log(f'Retrieved {len(playlists)} cached playlists for user "{self.__userid}".')
        else:
            while total > count:
                playlists["items"] += self.__spotipy.user_playlists(userid, limit=50, offset=count)[
                    "items"
                ]
                count += 50
            playlists = self.__prepare_playlist_listitems(playlists["items"])
            self.cache.set(cache_str, playlists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(playlists)} UNCACHED playlists for user "{self.__userid}".'
            )

        return playlists

    def __get_curuser_playlistids(self) -> List[str]:
        cache_str = f"spotify.userplaylistids.{self.__userid}"
        checksum = self.__library_checksum()
        playlist_ids = self.cache.get(cache_str, checksum=checksum)
        if playlist_ids is not None:
            cache_log(
                f'Retrieved {len(playlist_ids)} cached playlist ids for user "{self.__userid}".'
            )
            return playlist_ids

        playlists = self.__spotipy.current_user_playlists(limit=50, offset=0)
        count = len(playlists["items"])
        while playlists["total"] > count:
            playlists["items"] += self.__spotipy.current_user_playlists(limit=50, offset=count)[
                "items"
            ]
            count += 50
        playlist_ids = [playlist["id"] for playlist in playlists["items"]]
        self.cache.set(cache_str, playlist_ids, checksum=checksum, expiration=LIBRARY_CACHE_TIME)
        cache_log(
            f'Retrieved {len(playlist_ids)} UNCACHED playlist ids for user "{self.__userid}".'
        )
        return playlist_ids

    def browse_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        if self.__filter == "featured":
            playlists = self.__get_featured_playlists()
            xbmcplugin.setProperty(self.__addon_handle, "FolderName", playlists["message"])
            playlists = playlists["playlists"]["items"]
        else:
            xbmcplugin.setProperty(
                self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)
            )
            playlists = self.__get_user_playlists(self.__owner_id)

        self.__add_playlist_listitems(playlists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_playlists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_playlists})")

    def __get_new_releases(self):
        albums = self.__spotipy.new_releases(country=self.__user_country, limit=50, offset=0)
        count = len(albums["albums"]["items"])
        while albums["albums"]["total"] > count:
            albums["albums"]["items"] += self.__spotipy.new_releases(
                country=self.__user_country, limit=50, offset=count
            )["albums"]["items"]
            count += 50

        album_ids = []
        for album in albums["albums"]["items"]:
            album_ids.append(album["id"])
        albums = self.__prepare_album_listitems(album_ids)

        return albums

    def browse_new_releases(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle,
            "FolderName",
            self.__addon.getLocalizedString(ALL_NEW_RELEASES_STR_ID),
        )
        albums = self.__get_new_releases()
        self.__add_album_listitems(albums)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_albums:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_albums})")

    def __prepare_track_listitems(
        self, track_ids=None, tracks=None, playlist_details=None, album_details=None
    ) -> List[Dict[str, Any]]:
        if tracks is None:
            tracks = []
        if track_ids is None:
            track_ids = []

        new_tracks: List[Dict[str, Any]] = []

        # For tracks, we always get the full details unless full tracks already supplied.
        if track_ids and not tracks:
            for chunk in get_chunks(track_ids, 20):
                tracks += self.__spotipy.tracks(chunk, market=self.__user_country)["tracks"]

        for track in tracks:
            if track.get("track"):
                track = track["track"]
            if album_details:
                track["album"] = album_details
            if track.get("images"):
                thumb = track["images"][0]["url"]
            elif track.get("album", {}).get("images"):
                thumb = track["album"]["images"][0]["url"]
            else:
                thumb = "DefaultMusicSongs.png"
            track["thumb"] = thumb

            # Skip local tracks in playlists.
            if not track.get("id"):
                continue

            if "artists" in track:
                artists = []
                for artist in track["artists"]:
                    if artist["name"]:
                        artists.append(artist["name"])
                if artists:
                    track["artist"] = " / ".join(artists)
                    track["artistid"] = track["artists"][0]["id"]

            if "album" not in track:
                track["genre"] = []
                track["year"] = 1900
            else:
                track["genre"] = " / ".join(track["album"].get("genres", []))

                # Allow for 'release_date' being empty.
                release_date = (
                    "0" if "album" not in track else track["album"].get("release_date", "0")
                )
                track["year"] = (
                    1900
                    if not release_date
                    else int(track["album"].get("release_date", "0").split("-")[0])
                )

            track["rating"] = int(self.__get_track_rating(int(track.get("popularity", "0"))))

            if playlist_details:
                track["playlistid"] = playlist_details["id"]
                track["playlistownerid"] = playlist_details["owner"]["id"]
                track["playlistname"] = playlist_details["name"]

            new_tracks.append(track)

        return new_tracks

    def __get_playlist_track_context_menu_items(
        self, track, saved_track_ids: Set[str], followed_artist_ids: Set[str]
    ) -> List[Tuple[str, str]]:
        # Use original track id for actions when the track was relinked.
        if track.get("linked_from"):
            real_track_id = track["linked_from"]["id"]
            real_track_uri = track["linked_from"]["uri"]
        else:
            real_track_id = track["id"]
            real_track_uri = track["uri"]

        context_items = [
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            )
        ]

        if track["id"] in saved_track_ids:
            context_items.append(
                (
                    self.__addon.getLocalizedString(REMOVE_TRACKS_FROM_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_track.__name__}&trackid={real_track_id})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(SAVE_TRACKS_TO_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.save_track.__name__}&trackid={real_track_id})",
                )
            )

        if track.get("playlistownerid") == self.__userid:
            context_items.append(
                (
                    f"{self.__addon.getLocalizedString(REMOVE_FROM_PLAYLIST_STR_ID)}"
                    f" {track['playlistname']}",
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_track_from_playlist.__name__}&trackid="
                    f"{real_track_uri}&playlistid={track['playlistid']})",
                )
            )

        context_items.append(
            (
                xbmc.getLocalizedString(KODI_ADD_TO_PLAYLIST_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/"
                f"?action={self.add_track_to_playlist.__name__}&trackid={real_track_uri})",
            )
        )

        if "artistid" in track:
            context_items.append(
                (
                    self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.artist_top_tracks.__name__}&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_albums.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_SINGLES_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_singles.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_just_appears_on.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )
            context_items.append(
                (
                    self.__addon.getLocalizedString(EVERYTHING_FOR_ARTIST_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.browse_artist_everything.__name__}"
                    f"&artistid={track['artistid']})",
                )
            )

            if track["artistid"] in followed_artist_ids:
                context_items.append(
                    (
                        self.__addon.getLocalizedString(UNFOLLOW_ARTIST_STR_ID),
                        f"RunPlugin(plugin://{ADDON_ID}/"
                        f"?action={self.unfollow_artist.__name__}"
                        f"&artistid={track['artistid']})",
                    )
                )
            else:
                context_items.append(
                    (
                        self.__addon.getLocalizedString(FOLLOW_ARTIST_STR_ID),
                        f"RunPlugin(plugin://{ADDON_ID}/"
                        f"?action={self.follow_artist.__name__}&artistid={track['artistid']})",
                    )
                )

            context_items.append(
                (
                    self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                    f"Container.Update(plugin://{ADDON_ID}/"
                    f"?action={self.related_artists.__name__}&artistid={track['artistid']})",
                )
            )

        return context_items

    def __prepare_album_listitems(
        self, album_ids: List[str] = None, albums: List[Dict[str, Any]] = None
    ) -> List[Dict[str, Any]]:
        if albums is None:
            albums: List[Dict[str, Any]] = []
        if album_ids is None:
            album_ids = []
        if not albums and album_ids:
            # Get full info in chunks of 20.
            for chunk in get_chunks(album_ids, 20):
                albums += self.__spotipy.albums(chunk, market=self.__user_country)["albums"]

        # process listing
        for track in albums:
            if track.get("images"):
                track["thumb"] = track["images"][0]["url"]
            else:
                track["thumb"] = "DefaultMusicAlbums.png"

            track["url"] = self.__build_url(
                {"action": self.browse_album.__name__, "albumid": track["id"]}
            )

            artists = []
            for artist in track["artists"]:
                artists.append(artist["name"])
            track["artist"] = " / ".join(artists)
            track["genre"] = " / ".join(track["genres"])
            track["year"] = int(track["release_date"].split("-")[0])
            track["rating"] = str(self.__get_track_rating(track["popularity"]))
            track["artistid"] = track["artists"][0]["id"]

        return albums

    def __get_album_track_context_menu_items(
        self, track, saved_album_ids: Set[str]
    ) -> List[Tuple[str, str]]:
        context_items = [
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            ),
            (
                xbmc.getLocalizedString(KODI_BROWSE_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_album.__name__}&albumid={track['id']})",
            ),
            (
                self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.artist_top_tracks.__name__}&artistid={track['artistid']})",
            ),
            (
                self.__addon.getLocalizedString(EVERYTHING_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_everything.__name__}&artistid={track['artistid']})",
            ),
            (
                self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.related_artists.__name__}&artistid={track['artistid']})",
            ),
        ]

        if track["id"] in saved_album_ids:
            context_items.append(
                (
                    self.__addon.getLocalizedString(REMOVE_TRACKS_FROM_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.remove_album.__name__}&albumid={track['id']})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(SAVE_TRACKS_TO_MY_MUSIC_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.save_album.__name__}&albumid={track['id']})",
                )
            )

        return context_items

    def __add_album_listitems(
        self, albums: List[Dict[str, Any]], append_artist_to_label: bool = False
    ) -> None:
        saved_album_ids = set(self.__get_saved_album_ids())

        # Process listing.
        for track in albums:
            label = self.__get_track_name(track, append_artist_to_label)

            li = xbmcgui.ListItem(label, path=track["url"], offscreen=True)
            info_labels = {
                "title": track["name"],
                "genre": track["genre"],
                "year": track["year"],
                "album": track["name"],
                "artist": track["artist"],
                "rating": track["rating"],
            }
            li.setInfo(type="Music", infoLabels=info_labels)
            li.setArt({"thumb": track["thumb"]})
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.addContextMenuItems(
                self.__get_album_track_context_menu_items(track, saved_album_ids), True
            )
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=track["url"], listitem=li, isFolder=True
            )

    def __prepare_artist_listitems(self, artists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        for artist in artists:
            if not artist:
                return []
            if artist.get("artist"):
                artist = artist["artist"]
            if artist.get("images"):
                artist["thumb"] = artist["images"][0]["url"]
            else:
                artist["thumb"] = "DefaultMusicArtists.png"

            artist["url"] = self.__build_url(
                {
                    "action": self.browse_artist_just_albums_and_singles.__name__,
                    "artistid": artist["id"],
                }
            )

            artist["genre"] = " / ".join(artist["genres"])
            artist["rating"] = str(self.__get_track_rating(artist["popularity"]))
            artist["followerslabel"] = f"{artist['followers']['total']} followers"

        return artists

    def __get_artist_context_menu_items(
        self, artist, followed_artist_ids: Set[str]
    ) -> List[Tuple[str, str]]:
        context_items = [
            (
                xbmc.getLocalizedString(ALL_ALBUMS_AND_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update({artist['url']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_ALBUMS_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_albums.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_SINGLES_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_singles.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ALL_APPEARS_ON_FOR_ARTIST_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.browse_artist_just_appears_on.__name__}&artistid={artist['id']})",
            ),
            (
                self.__addon.getLocalizedString(ARTIST_TOP_TRACKS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.artist_top_tracks.__name__}&artistid={artist['id']})",
            ),
        ]

        if artist["id"] in followed_artist_ids:
            context_items.append(
                (
                    self.__addon.getLocalizedString(UNFOLLOW_ARTIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.unfollow_artist.__name__}&artistid={artist['id']})",
                )
            )
        else:
            context_items.append(
                (
                    self.__addon.getLocalizedString(FOLLOW_ARTIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.follow_artist.__name__}&artistid={artist['id']})",
                )
            )

        context_items.append(
            (
                self.__addon.getLocalizedString(RELATED_ARTISTS_STR_ID),
                f"Container.Update(plugin://{ADDON_ID}/"
                f"?action={self.related_artists.__name__}&artistid={artist['id']})",
            )
        )

        return context_items

    def __add_artist_listitems(self, artists: List[Dict[str, Any]]) -> None:
        followed_artist_ids = self.__get_followed_artist_ids()

        for item in artists:
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            info_labels = {
                "title": item["name"],
                "genre": item["genre"],
                "artist": item["name"],
                "rating": item["rating"],
            }
            li.setInfo(type="Music", infoLabels=info_labels)
            li.setArt({"thumb": item["thumb"]})
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")
            li.setLabel2(item["followerslabel"])
            li.addContextMenuItems(
                self.__get_artist_context_menu_items(item, followed_artist_ids), True
            )
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle,
                url=item["url"],
                listitem=li,
                isFolder=True,
                totalItems=len(artists),
            )

    def __prepare_playlist_listitems(self, playlists: List[Dict[str, Any]]) -> List[Dict[str, Any]]:
        playlists2 = []

        for playlist in playlists:
            if not playlist:
                continue

            if playlist.get("images"):
                playlist["thumb"] = playlist["images"][0]["url"]
            else:
                playlist["thumb"] = "DefaultMusicAlbums.png"

            playlist["url"] = self.__build_url(
                {
                    "action": self.browse_playlist.__name__,
                    "playlistid": playlist["id"],
                    "ownerid": playlist["owner"]["id"],
                }
            )

            playlists2.append(playlist)

        return playlists2

    def __get_playlist_context_menu_items(
        self, playlist, followed_playlist_ids: Set[str]
    ) -> List[Tuple[str, str]]:
        contextitems = [
            (
                xbmc.getLocalizedString(KODI_PLAY_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/"
                f"?action={self.play_playlist.__name__}&playlistid={playlist['id']}"
                f"&ownerid={playlist['owner']['id']})",
            ),
            (
                self.__addon.getLocalizedString(REFRESH_LISTING_STR_ID),
                f"RunPlugin(plugin://{ADDON_ID}/" f"?action={self.refresh_listing.__name__})",
            ),
        ]

        if playlist["owner"]["id"] != self.__userid and playlist["id"] in followed_playlist_ids:
            contextitems.append(
                (
                    self.__addon.getLocalizedString(UNFOLLOW_PLAYLIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.unfollow_playlist.__name__}&playlistid={playlist['id']}"
                    f"&ownerid={playlist['owner']['id']})",
                )
            )
        elif playlist["owner"]["id"] != self.__userid:
            contextitems.append(
                (
                    self.__addon.getLocalizedString(FOLLOW_PLAYLIST_STR_ID),
                    f"RunPlugin(plugin://{ADDON_ID}/"
                    f"?action={self.follow_playlist.__name__}&playlistid={playlist['id']}"
                    f"&ownerid={playlist['owner']['id']})",
                )
            )

        return contextitems

    def __add_playlist_listitems(self, playlists: List[Dict[str, Any]]) -> None:
        followed_playlist_ids = set(self.__get_curuser_playlistids())

        for item in playlists:
            li = xbmcgui.ListItem(item["name"], path=item["url"], offscreen=True)
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")

            li.addContextMenuItems(
                self.__get_playlist_context_menu_items(item, followed_playlist_ids), True
            )
            li.setArt(
                {
                    "fanart": os.path.join(self.__addon_icon_path, "fanart.jpg"),
                    "thumb": item["thumb"],
                }
            )
            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=item["url"], listitem=li, isFolder=True
            )

    def browse_artist_everything(self) -> None:
        self.browse_artist_albums(album_type="album,single,appears_on,compilation")

    def browse_artist_just_albums(self) -> None:
        self.browse_artist_albums(album_type="album,compilation")

    def browse_artist_just_singles(self) -> None:
        self.browse_artist_albums(album_type="single")

    def browse_artist_just_albums_and_singles(self) -> None:
        self.browse_artist_albums(album_type="album,single")

    def browse_artist_just_compilations(self) -> None:
        self.browse_artist_albums(album_type="compilation")

    def browse_artist_just_appears_on(self) -> None:
        self.browse_artist_albums(album_type="appears_on")

    def browse_artist_albums(self, album_type: str) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )
        artist_albums = self.__spotipy.artist_albums(
            self.__artist_id,
            album_type=album_type,
            country=self.__user_country,
            limit=50,
            offset=0,
        )
        count = len(artist_albums["items"])
        albumids = []
        while artist_albums["total"] > count:
            artist_albums["items"] += self.__spotipy.artist_albums(
                self.__artist_id,
                album_type=album_type,
                country=self.__user_country,
                limit=50,
                offset=count,
            )["items"]
            count += 50
        for album in artist_albums["items"]:
            albumids.append(album["id"])
        albums = self.__prepare_album_listitems(albumids)
        self.__add_album_listitems(albums)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_albums:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_albums})")

    def __get_saved_album_ids(self) -> List[str]:
        cache_str = f"spotify-savedalbumids.{self.__userid}"
        checksum = self.__library_checksum()
        album_ids = self.cache.get(cache_str, checksum=checksum)
        if album_ids is not None:
            cache_log(f'Retrieved {len(album_ids)} cached album ids for user "{self.__userid}".')
            return album_ids

        albums = self.__spotipy.current_user_saved_albums(limit=50, offset=0)
        count = len(albums["items"])
        while albums["total"] > count:
            albums["items"] += self.__spotipy.current_user_saved_albums(limit=50, offset=count)[
                "items"
            ]
            count += 50
        album_ids = [album["album"]["id"] for album in albums["items"]]
        self.cache.set(cache_str, album_ids, checksum=checksum, expiration=LIBRARY_CACHE_TIME)
        cache_log(f'Retrieved {len(album_ids)} UNCACHED album ids for user "{self.__userid}".')

        return album_ids

    def __get_saved_albums(self) -> List[Dict[str, Any]]:
        album_ids = self.__get_saved_album_ids()
        cache_str = f"spotify.savedalbums.{self.__userid}"
        checksum = f"{self.__library_checksum()}-{len(album_ids)}"
        albums = self.cache.get(cache_str, checksum=checksum)
        if albums:
            cache_log(f'Retrieved {len(albums)} cached albums for user "{self.__userid}".')
        else:
            albums = self.__prepare_album_listitems(album_ids)
            self.cache.set(cache_str, albums, checksum=checksum)
            cache_log(f'Retrieved {_get_len(albums)} UNCACHED albums for user "{self.__userid}".')
        return albums

    def browse_saved_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )
        albums = self.__get_saved_albums()
        self.__add_album_listitems(albums, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_ALBUM_IGNORE_THE)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_VIDEO_YEAR)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_SONG_RATING)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        xbmcplugin.setContent(self.__addon_handle, "albums")
        if self.default_view_albums:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_albums})")

    def __get_saved_track_ids(self) -> List[str]:
        cache_str = f"spotify.savedtracksids.{self.__userid}"
        checksum = self.__library_checksum()
        track_ids = self.cache.get(cache_str, checksum=checksum)
        if track_ids is not None:
            cache_log(
                f'Retrieved {len(track_ids)} cached saved track ids for user "{self.__userid}".'
            )
            return track_ids

        # Get from api.
        saved_tracks = self.__spotipy.current_user_saved_tracks(
            limit=50, offset=0, market=self.__user_country
        )
        count = len(saved_tracks["items"])
        while saved_tracks["total"] > count:
            saved_tracks["items"] += self.__spotipy.current_user_saved_tracks(
                limit=50, offset=count, market=self.__user_country
            )["items"]
            count += 50
        track_ids = [track["track"]["id"] for track in saved_tracks["items"]]
        self.cache.set(cache_str, track_ids, checksum=checksum, expiration=LIBRARY_CACHE_TIME)
        cache_log(
            f'Retrieved {len(track_ids)} UNCACHED saved track ids for user "{self.__userid}".'
        )

        return track_ids

    def __get_saved_tracks(self):
        # Get from cache first.
        track_ids = self.__get_saved_track_ids()
        cache_str = f"spotify.savedtracks.{self.__userid}"

        checksum = f"{self.__library_checksum()}-{len(track_ids)}"
        tracks = self.cache.get(cache_str, checksum=checksum)
        if tracks:
            cache_log(f'Retrieved {len(tracks)} cached saved tracks for user "{self.__userid}".')
        else:
            # Get from api.
            tracks = self.__prepare_track_listitems(track_ids)
            self.cache.set(cache_str, tracks, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(tracks)} UNCACHED saved tracks for user "{self.__userid}".'
            )

        return tracks

    def browse_saved_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_SONGS_STR_ID)
        )
        tracks = self.__get_saved_tracks()
        self.__add_track_listitems(tracks, True)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def __get_saved_artists(self) -> List[Dict[str, Any]]:
        saved_albums = self.__get_saved_albums()
        followed_artists = self.__get_followed_artists()
        cache_str = f"spotify.savedartists.{self.__userid}"
        checksum = f"{self.__library_checksum()}-{len(saved_albums)}-{len(followed_artists)}"
        artists = self.cache.get(cache_str, checksum=checksum)
        if artists:
            cache_log(f'Retrieved {len(artists)} cached saved artists for user "{self.__userid}".')
        else:
            all_artist_ids = []
            artists = []
            # extract the artists from all saved albums
            for item in saved_albums:
                for artist in item["artists"]:
                    if artist["id"] not in all_artist_ids:
                        all_artist_ids.append(artist["id"])
            for chunk in get_chunks(all_artist_ids, 50):
                artists += self.__prepare_artist_listitems(self.__spotipy.artists(chunk)["artists"])
            # append artists that are followed
            for artist in followed_artists:
                if not artist["id"] in all_artist_ids:
                    artists.append(artist)
            self.cache.set(cache_str, artists, checksum=checksum)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED saved artists for user "{self.__userid}".'
            )

        return artists

    def browse_saved_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )
        artists = self.__get_saved_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_artists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_artists})")

    def __get_followed_artists(self) -> List[Dict[str, Any]]:
        cache_str = f"spotify.followedartists.{self.__userid}"
        checksum = self.__library_checksum()

        cached_artists = self.cache.get(cache_str, checksum=checksum)
        if cached_artists is not None:
            artists = cached_artists
            cache_log(
                f'Retrieved {len(artists)} cached followed artists for user "{self.__userid}".'
            )
        else:
            artists = self.__spotipy.current_user_followed_artists(limit=50)
            count = len(artists["artists"]["items"])
            after = artists["artists"]["cursors"]["after"]
            while artists["artists"]["total"] > count:
                result = self.__spotipy.current_user_followed_artists(limit=50, after=after)
                artists["artists"]["items"] += result["artists"]["items"]
                after = result["artists"]["cursors"]["after"]
                count += 50
            artists = self.__prepare_artist_listitems(artists["artists"]["items"])
            self.cache.set(cache_str, artists, checksum=checksum, expiration=LIBRARY_CACHE_TIME)
            cache_log(
                f'Retrieved {_get_len(artists)} UNCACHED followed artists for user "{self.__userid}".'
            )

        return artists

    def __get_followed_artist_ids(self) -> Set[str]:
        return {artist["id"] for artist in self.__get_followed_artists()}

    def browse_followed_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )
        artists = self.__get_followed_artists()
        self.__add_artist_listitems(artists)
        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_TITLE)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)
        if self.default_view_artists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_artists})")

    def search_artists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "artists")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"artist:{self.__artist_id}",
            type="artist",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        artists = self.__prepare_artist_listitems(result["artists"]["items"])
        self.__add_artist_listitems(artists)
        self.__add_next_button(result["artists"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        if self.default_view_artists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_artists})")

    def search_tracks(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "songs")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_SONGS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"track:{self.__track_id}",
            type="track",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        tracks = self.__prepare_track_listitems(tracks=result["tracks"]["items"])
        self.__add_track_listitems(tracks, True)
        self.__add_next_button(result["tracks"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        if self.default_view_songs:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_songs})")

    def search_albums(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "albums")
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)
        )

        result = self.__spotipy.search(
            q=f"album:{self.__album_id}",
            type="album",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        album_ids = []
        for album in result["albums"]["items"]:
            album_ids.append(album["id"])
        albums = self.__prepare_album_listitems(album_ids)
        self.__add_album_listitems(albums, True)
        self.__add_next_button(result["albums"]["total"])

        xbmcplugin.addSortMethod(self.__addon_handle, xbmcplugin.SORT_METHOD_UNSORTED)
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        if self.default_view_albums:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_albums})")

    def search_playlists(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")

        result = self.__spotipy.search(
            q=self.__playlist_id,
            type="playlist",
            limit=self.__limit,
            offset=self.__offset,
            market=self.__user_country,
        )

        log_msg(result)
        xbmcplugin.setProperty(
            self.__addon_handle, "FolderName", xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)
        )
        playlists = self.__prepare_playlist_listitems(result["playlists"]["items"])
        self.__add_playlist_listitems(playlists)
        self.__add_next_button(result["playlists"]["total"])
        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

        if self.default_view_playlists:
            xbmc.executebuiltin(f"Container.SetViewMode({self.default_view_playlists})")

    def search(self) -> None:
        xbmcplugin.setContent(self.__addon_handle, "files")
        xbmcplugin.setPluginCategory(
            self.__addon_handle, xbmc.getLocalizedString(KODI_SEARCH_RESULTS_STR_ID)
        )

        kb = xbmc.Keyboard("", xbmc.getLocalizedString(KODI_ENTER_SEARCH_STRING_STR_ID))
        kb.doModal()
        if kb.isConfirmed():
            value = kb.getText()
            items = []
            result = self.__spotipy.search(
                q=f"{value}",
                type="artist,album,track,playlist",
                limit=1,
                market=self.__user_country,
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_ARTISTS_STR_ID)}"
                    f" ({result['artists']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_artists.__name__}&artistid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_PLAYLISTS_STR_ID)}"
                    f" ({result['playlists']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_playlists.__name__}&playlistid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_ALBUMS_STR_ID)} ({result['albums']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_albums.__name__}&albumid={value}",
                )
            )
            items.append(
                (
                    f"{xbmc.getLocalizedString(KODI_SONGS_STR_ID)} ({result['tracks']['total']})",
                    f"plugin://{ADDON_ID}/"
                    f"?action={self.search_tracks.__name__}&trackid={value}",
                )
            )
            for item in items:
                li = xbmcgui.ListItem(item[0], path=item[1])
                li.setProperty("do_not_analyze", "true")
                li.setProperty("IsPlayable", "false")
                li.addContextMenuItems([], True)
                xbmcplugin.addDirectoryItem(
                    handle=self.__addon_handle, url=item[1], listitem=li, isFolder=True
                )

        xbmcplugin.endOfDirectory(handle=self.__addon_handle)

    def __add_next_button(self, list_total: int) -> None:
        # Adds a next button if needed.
        params = self.__params
        if list_total > self.__offset + self.__limit:
            params["offset"] = [str(self.__offset + self.__limit)]
            url = f"plugin://{ADDON_ID}/"

            for key, value in list(params.items()):
                if key == "action":
                    url += f"?{key}={value[0]}"
                elif key == "offset":
                    url += f"&{key}={value}"
                else:
                    url += f"&{key}={value[0]}"

            li = xbmcgui.ListItem(xbmc.getLocalizedString(KODI_NEXT_PAGE_STR_ID), path=url)
            li.setProperty("do_not_analyze", "true")
            li.setProperty("IsPlayable", "false")

            xbmcplugin.addDirectoryItem(
                handle=self.__addon_handle, url=url, listitem=li, isFolder=True
            )

    def __precache_library(self) -> None:
        if not self.__win.getProperty("Spotify.PreCachedItems"):
            monitor = xbmc.Monitor()
            self.__win.setProperty("Spotify.PreCachedItems", "busy")
            user_playlists = self.__get_user_playlists(self.__userid)
            for playlist in user_playlists:
                if monitor.waitForAbort(PRECACHE_PAUSE_SECONDS):
                    return
                self.__get_playlist_details(playlist["id"], playlist)
            if monitor.waitForAbort(PRECACHE_PAUSE_SECONDS):
                return
            self.__get_saved_albums()
            if monitor.waitForAbort(PRECACHE_PAUSE_SECONDS):
                return
            self.__get_saved_artists()
            if monitor.waitForAbort(PRECACHE_PAUSE_SECONDS):
                return
            self.__get_saved_tracks()
            del monitor
            self.__win.setProperty("Spotify.PreCachedItems", "done")

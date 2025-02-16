import obspython as obs
from pathlib import Path
from enum import Enum, auto
import json
from uuid import uuid4

NO_SOURCE_SELECTED = "--No Source Selected--"

MPS_ID = "media_playlist_source_codeyan"
VLC_ID = "vlc_source"
SLIDESHOW_ID = "slideshow"

SOURCES_LIST_KEY = {
    MPS_ID: "playlist",
    VLC_ID: "playlist",
    SLIDESHOW_ID: "files",
}


class ScriptProperties:
    list_src = "list_src"
    refresh_list = "refresh_list"
    check_interval = "check_interval"
    directory = "directory"
    sort_mode = "sort_mode"
    sort_order = "sort_order"
    update_only_when_stopped = "update_only_when_stopped"


class SortMode(Enum):
    datetime_modified = 1
    filename = auto()
    filename_and_extension = auto()


sort_key_functions = {
    SortMode.datetime_modified: lambda x: Path(x["value"]).stat().st_mtime_ns,
    SortMode.filename: lambda x: Path(x["value"]).stem,
    SortMode.filename_and_extension: lambda x: Path(x["value"]).name,
}

list_weak_source = None
list_source_name = ""
check_interval = 0
directory: Path = None
sort_mode: SortMode
is_descending = False
update_only_when_stopped = False

script_settings = None


def frontend_event_cb(event):
    if (
        event == obs.OBS_FRONTEND_EVENT_FINISHED_LOADING
        or event == obs.OBS_FRONTEND_EVENT_SCENE_COLLECTION_CHANGED
    ):
        # Scripts are loaded before the sources are loaded, so force update after load so that we make the weak source
        # of currently selected source, otherwise the first script_update won't find the source
        script_update(None)
        obs.remove_current_callback()


def is_valid_source(source):
    return source and obs.obs_source_get_unversioned_id(source) in [
        MPS_ID,
        VLC_ID,
        SLIDESHOW_ID,
    ]


def script_description():
    return (
        "Updates a media list source (Media Playlist Source, VLC Video Source, Image Slideshow) with the files in the specified directory while sorting them."
        "<br>Due to scripting limitations, if you rename the sources that are selected, the script does update with the new name,"
        "<br>but the change won't show up in the Scripts menu unless you select a different script and select this script again."
    )


def refresh_lists(props, prop):
    list_src_list = obs.obs_properties_get(props, ScriptProperties.list_src)
    obs.obs_property_list_clear(list_src_list)

    sources = obs.obs_enum_sources()
    valid_sources = [NO_SOURCE_SELECTED]
    for source in sources:
        if is_valid_source(source):
            valid_sources.append(obs.obs_source_get_name(source))
    obs.source_list_release(sources)

    for source_name in valid_sources:
        obs.obs_property_list_add_string(list_src_list, source_name, source_name)

    return True  # Refresh properties


def script_properties():
    props = obs.obs_properties_create()
    obs.obs_properties_add_list(
        props,
        ScriptProperties.list_src,
        "List Sources",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_STRING,
    )
    obs.obs_properties_add_button(
        props, ScriptProperties.refresh_list, "Refresh Source Lists", refresh_lists
    )
    obs.obs_properties_add_int(
        props, ScriptProperties.check_interval, "Check Interval", 100, 2**16 - 1, 100
    )
    obs.obs_properties_add_path(
        props,
        ScriptProperties.directory,
        "Directory",
        obs.OBS_PATH_DIRECTORY,
        None,
        None,
    )

    p = obs.obs_properties_add_list(
        props,
        ScriptProperties.sort_mode,
        "Sort by",
        obs.OBS_COMBO_TYPE_LIST,
        obs.OBS_COMBO_FORMAT_INT,
    )
    obs.obs_property_list_add_int(
        p, "Datetime Modified", SortMode.datetime_modified.value
    )
    obs.obs_property_list_add_int(p, "Filename", SortMode.filename.value)
    obs.obs_property_list_add_int(
        p, "Filename & Extension", SortMode.filename_and_extension.value
    )

    obs.obs_properties_add_bool(props, ScriptProperties.sort_order, "Descending")
    p = obs.obs_properties_add_bool(
        props, ScriptProperties.update_only_when_stopped, "Update only when stopped"
    )
    obs.obs_property_set_long_description(
        p,
        """This option is needed only for VLC Video Source and Image Slideshow,
as Media Playlist Source can handle the file changes without stopping the currently playing file.
However, keep in mind that VLC Video Source and Image Slideshow WILL play when the settings are updated.""",
    )

    refresh_lists(props, None)

    return props


def script_update(settings):
    global list_weak_source
    global list_source_name
    global check_interval
    global directory
    global sort_mode
    global is_descending
    global update_only_when_stopped
    global script_settings

    new_list_source_name = ""
    force_update = False
    if settings:  # If settings is None, the update is forced by `frontend_event_cb`
        new_list_source_name = obs.obs_data_get_string(
            settings, ScriptProperties.list_src
        )
        new_check_interval = obs.obs_data_get_int(
            settings, ScriptProperties.check_interval
        )
        directory = Path(obs.obs_data_get_string(settings, ScriptProperties.directory))
        i = obs.obs_data_get_int(settings, ScriptProperties.sort_mode)
        if any(x.value == i for x in SortMode):
            sort_mode = SortMode(i)
        else:
            print(f"Invalid SortMode value: {i}")

        is_descending = obs.obs_data_get_bool(settings, ScriptProperties.sort_order)
        update_only_when_stopped = obs.obs_data_get_bool(
            settings, ScriptProperties.update_only_when_stopped
        )

        if new_check_interval != check_interval:
            check_interval = new_check_interval
            obs.timer_remove(on_timer)
            obs.timer_add(on_timer, check_interval)
    else:
        force_update = True
        new_list_source_name = list_source_name

    if force_update or (
        new_list_source_name and new_list_source_name != list_source_name
    ):
        # Disconnect previous callbacks
        list_source = obs.obs_weak_source_get_source(list_weak_source)
        if list_source:
            list_sh = obs.obs_source_get_signal_handler(list_source)
            obs.signal_handler_disconnect(list_sh, "rename", on_rename)
            obs.obs_source_release(list_source)
        list_source_name = new_list_source_name

        obs.obs_weak_source_release(list_weak_source)
        list_weak_source = None

        if list_source_name == NO_SOURCE_SELECTED:
            return

        list_source = obs.obs_get_source_by_name(list_source_name)

        if is_valid_source(list_source):
            list_sh = obs.obs_source_get_signal_handler(list_source)
            obs.signal_handler_connect(list_sh, "rename", on_rename)

            # Weak references let us keep a reference in a global variable, without
            # preventing the source from being destroyed.
            list_weak_source = obs.obs_source_get_weak_source(list_source)
        obs.obs_source_release(list_source)

    script_settings = settings


def script_defaults(settings):
    obs.obs_data_set_default_int(settings, ScriptProperties.check_interval, 10000)
    obs.obs_data_set_default_int(
        settings, ScriptProperties.sort_mode, SortMode.datetime_modified.value
    )


def script_load(settings):
    global script_settings

    obs.obs_frontend_add_event_callback(frontend_event_cb)

    script_settings = settings


def script_unload():
    obs.obs_weak_source_release(list_weak_source)


def on_rename(calldata):
    global list_source_name

    new_name = obs.calldata_string(calldata, "new_name")
    obs.obs_data_addref(script_settings)
    obs.obs_data_set_string(script_settings, ScriptProperties.list_src, new_name)
    list_source_name = new_name
    obs.obs_data_release(script_settings)


def on_timer():
    global directory
    global sort_mode
    global is_descending
    global update_only_when_stopped
    global list_weak_source
    global list_source_name

    if not directory or not directory.exists():
        return

    children: list[Path] = []
    for sub_path in directory.iterdir():
        if sub_path.is_file:
            children.append(sub_path)

    list_source = obs.obs_weak_source_get_source(list_weak_source)
    if not list_source:
        obs.obs_weak_source_release(list_weak_source)
        list_weak_source = None

        list_source = obs.obs_get_source_by_name(list_source_name)
        if not list_source:
            return

        list_weak_source = obs.obs_source_get_weak_source(list_source)

    state = obs.obs_source_media_get_state(list_source)

    if (not update_only_when_stopped) or (
        state not in [obs.OBS_MEDIA_STATE_PLAYING, obs.OBS_MEDIA_STATE_PAUSED]
    ):
        settings = obs.obs_source_get_settings(list_source)
        id = obs.obs_source_get_unversioned_id(list_source)

        # Get files from source settings
        items = []
        files_array = obs.obs_data_get_array(settings, SOURCES_LIST_KEY[id])
        files_array_count = obs.obs_data_array_count(files_array)
        for i in range(files_array_count):
            item = obs.obs_data_array_item(files_array, i)
            item_parsed = json.loads(obs.obs_data_get_json(item))
            items.append(item_parsed)
            obs.obs_data_release(item)

        obs.obs_data_array_release(files_array)
        obs.obs_data_release(settings)

        orig_items = items.copy()

        # Remove non-existent items
        for i, item in enumerate(reversed(items)):
            item_as_path = Path(item["value"])
            if not item_as_path.exists():
                items.pop(i)

        # Add items
        for child in children:
            found = False
            # Find in items in settings
            for item in items:
                if child == Path(item["value"]):
                    found = True
                    break

            if not found:
                items.append(
                    {
                        "value": str(child),
                        "uuid": str(uuid4()),
                        "selected": False,
                        "hidden": False,
                    }
                )

        items.sort(key=sort_key_functions[sort_mode], reverse=is_descending)

        if orig_items != items:
            new_settings = obs.obs_data_create_from_json(
                json.dumps({SOURCES_LIST_KEY[id]: items})
            )
            obs.obs_source_update(list_source, new_settings)
            obs.obs_data_release(new_settings)

    obs.obs_source_release(list_source)

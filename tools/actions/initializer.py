# Copyright 2021 Erfan Abdi
# SPDX-License-Identifier: GPL-3.0-or-later
import logging
import os
from tools import helpers
import tools.config

import sys
import threading
import multiprocessing
import select
import queue

def is_initialized(args):
    return os.path.isfile(args.config) and os.path.isdir(tools.config.defaults["rootfs"])

def get_vendor_type(args):
    vndk_str = helpers.props.host_get(args, "ro.vndk.version")
    ret = "MAINLINE"
    if vndk_str != "":
        vndk = int(vndk_str)
        if vndk > 19:
            ret = "HALIUM_" + str(vndk - 19)

    return ret

def setup_config(args):
    cfg = tools.config.load(args)
    args.arch = helpers.arch.host()
    cfg["waydroid"]["arch"] = args.arch

    preinstalled_images_paths = tools.config.defaults["preinstalled_images_paths"]
    if not args.images_path:
        for preinstalled_images in preinstalled_images_paths:
            if os.path.isdir(preinstalled_images):
                if os.path.isfile(preinstalled_images + "/system.img") and os.path.isfile(preinstalled_images + "/vendor.img"):
                    args.images_path = preinstalled_images
                    break
                else:
                    logging.warning("Found directory {} but missing system or vendor image, ignoring...".format(preinstalled_images))

    if not args.images_path:
        args.images_path = tools.config.defaults["images_path"]
    cfg["waydroid"]["images_path"] = args.images_path

    channels_cfg = tools.config.load_channels()
    if not args.system_channel:
        args.system_channel = channels_cfg["channels"]["system_channel"]
    if not args.vendor_channel:
        args.vendor_channel = channels_cfg["channels"]["vendor_channel"]
    if not args.rom_type:
        args.rom_type = channels_cfg["channels"]["rom_type"]
    if not args.system_type:
        args.system_type = channels_cfg["channels"]["system_type"]

    args.system_ota = args.system_channel + "/" + args.rom_type + \
        "/waydroid_" + args.arch + "/" + args.system_type + ".json"
    system_request = helpers.http.retrieve(args.system_ota)
    if system_request[0] != 200:
        if args.images_path not in preinstalled_images_paths:
            raise ValueError(
                "Failed to get system OTA channel: {}, error: {}".format(args.system_ota, system_request[0]))
        else:
            args.system_ota = "None"

    device_codename = helpers.props.host_get(args, "ro.product.device")
    args.vendor_type = None
    for vendor in [device_codename, get_vendor_type(args)]:
        vendor_ota = args.vendor_channel + "/waydroid_" + \
            args.arch + "/" + vendor.replace(" ", "_") + ".json"
        vendor_request = helpers.http.retrieve(vendor_ota)
        if vendor_request[0] == 200:
            args.vendor_type = vendor
            args.vendor_ota = vendor_ota
            break

    if not args.vendor_type:
        if args.images_path not in preinstalled_images_paths:
            raise ValueError(
                "Failed to get vendor OTA channel: {}".format(vendor_ota))
        else:
            args.vendor_ota = "None"
            args.vendor_type = get_vendor_type(args)

    if args.system_ota != cfg["waydroid"].get("system_ota"):
        cfg["waydroid"]["system_datetime"] = tools.config.defaults["system_datetime"]
    if args.vendor_ota != cfg["waydroid"].get("vendor_ota"):
        cfg["waydroid"]["vendor_datetime"] = tools.config.defaults["vendor_datetime"]

    cfg["waydroid"]["vendor_type"] = args.vendor_type
    cfg["waydroid"]["system_ota"] = args.system_ota
    cfg["waydroid"]["vendor_ota"] = args.vendor_ota
    helpers.drivers.setupBinderNodes(args)
    cfg["waydroid"]["binder"] = args.BINDER_DRIVER
    cfg["waydroid"]["vndbinder"] = args.VNDBINDER_DRIVER
    cfg["waydroid"]["hwbinder"] = args.HWBINDER_DRIVER
    tools.config.save(args, cfg)

def init(args):
    if not is_initialized(args) or args.force:
        setup_config(args)
        status = "STOPPED"
        if os.path.exists(tools.config.defaults["lxc"] + "/waydroid"):
            status = helpers.lxc.status(args)
        if status != "STOPPED":
            logging.info("Stopping container")
            helpers.lxc.stop(args)
        helpers.images.umount_rootfs(args)
        if args.images_path not in tools.config.defaults["preinstalled_images_paths"]:
            helpers.images.get(args)
        if not os.path.isdir(tools.config.defaults["rootfs"]):
            os.mkdir(tools.config.defaults["rootfs"])
        helpers.lxc.setup_host_perms(args)
        helpers.lxc.set_lxc_config(args)
        helpers.lxc.make_base_props(args)
        if status != "STOPPED":
            logging.info("Starting container")
            helpers.images.mount_rootfs(args, args.images_path)
            helpers.lxc.start(args)

        helpers.ipc.notify(channel="init", msg="done")
    else:
        logging.info("Already initialized")

def wait_for_init(args):
    helpers.ipc.create_channel("init")
    helpers.ipc.create_channel("remote_init_output")
    while True:
        print('WayDroid waiting for initialization...')
        msg = helpers.ipc.read_one(channel="init")
        if msg == "done":
            if is_initialized(args):
                break
            else:
                continue
        if msg.startswith("cmd"):
            remote_init_server(args, msg)
            continue

def background_remote_init_process(args):
    with helpers.ipc.open_channel("remote_init_output", "wb") as channel_out:
        class StdoutRedirect(logging.StreamHandler):
            def write(self, s):
                channel_out.write(str.encode(s))
            def flush(self):
                pass
            def emit(self, record):
                if record.levelno >= logging.INFO:
                    self.write(self.format(record) + self.terminator)

        out = StdoutRedirect()
        sys.stdout = sys.stderr = out
        logging.getLogger().addHandler(out)

        ctl_queue = queue.Queue()
        def try_init(args):
            try:
                init(args)
            except Exception as e:
                print(str(e))
            finally:
                ctl_queue.put(0)

        def poll_pipe():
            poller = select.poll()
            poller.register(channel_out, select.POLLERR)
            poller.poll()
            # When reaching here the client was terminated
            ctl_queue.put(0)

        init_thread = threading.Thread(target=try_init, args=(args,))
        init_thread.daemon = True
        init_thread.start()

        poll_thread = threading.Thread(target=poll_pipe)
        poll_thread.daemon = True
        poll_thread.start()

        # Join any one of the two threads
        # Then exit the subprocess to kill the remaining thread.
        # Can you believe this is the only way to kill a thread in python???
        ctl_queue.get()

        sys.stdout = sys.__stdout__
        sys.stderr = sys.__stderr__
        logging.getLogger().removeHandler(out)

def remote_init_server(args, cmd):
    params = cmd.split('\f')[1:]
    args.force = True
    args.images_path = ""
    args.rom_type = ""
    args.system_channel = params[0]
    args.vendor_channel = params[1]
    args.system_type = params[2]

    p = multiprocessing.Process(target=background_remote_init_process, args=(args,))
    p.daemon = True
    p.start()
    p.join()

def remote_init_client(args):
    # Local imports cause Gtk is intrusive
    import gi
    gi.require_version("Gtk", "3.0")
    from gi.repository import Gtk, GLib

    if is_initialized(args):
        helpers.ipc.notify(channel="init", msg="done")
        return

    def notify_and_quit(caller):
        if is_initialized(args):
            helpers.ipc.notify(channel="init", msg="done")
        GLib.idle_add(Gtk.main_quit)

    class WaydroidInitWindow(Gtk.Window):
        def __init__(self):
            super().__init__(title="Initialize Waydroid")
            self.set_default_size(600, 250)
            self.set_icon_from_file(tools.config.tools_src + "/data/AppIcon.png")

            grid = Gtk.Grid(row_spacing=6, column_spacing=6, margin=10, column_homogeneous=True)
            grid.set_hexpand(True)
            grid.set_vexpand(True)
            self.add(grid)

            sysOtaLabel = Gtk.Label("System OTA")
            sysOtaEntry = Gtk.Entry()
            sysOtaEntry.set_text(tools.config.channels_defaults["system_channel"])
            grid.attach(sysOtaLabel, 0, 0, 1, 1)
            grid.attach_next_to(sysOtaEntry ,sysOtaLabel, Gtk.PositionType.RIGHT, 2, 1)
            self.sysOta = sysOtaEntry.get_buffer()

            vndOtaLabel = Gtk.Label("Vendor OTA")
            vndOtaEntry = Gtk.Entry()
            vndOtaEntry.set_text(tools.config.channels_defaults["vendor_channel"])
            grid.attach(vndOtaLabel, 0, 1, 1, 1)
            grid.attach_next_to(vndOtaEntry, vndOtaLabel, Gtk.PositionType.RIGHT, 2, 1)
            self.vndOta = vndOtaEntry.get_buffer()

            sysTypeLabel = Gtk.Label("Android Type")
            sysTypeCombo = Gtk.ComboBoxText()
            sysTypeCombo.set_entry_text_column(0)
            for t in ["VANILLA", "GAPPS"]:
                sysTypeCombo.append_text(t)
            sysTypeCombo.set_active(0)
            grid.attach(sysTypeLabel, 0, 2, 1, 1)
            grid.attach_next_to(sysTypeCombo, sysTypeLabel, Gtk.PositionType.RIGHT, 2, 1)
            self.sysType = sysTypeCombo

            downloadBtn = Gtk.Button("Download")
            downloadBtn.connect("clicked", self.on_download_btn_clicked)
            grid.attach(downloadBtn, 1,3,1,1)
            self.downloadBtn = downloadBtn

            doneBtn = Gtk.Button("Done")
            doneBtn.connect("clicked", lambda x: self.destroy())
            doneBtn.get_style_context().add_class('suggested-action')
            grid.attach_next_to(doneBtn, downloadBtn, Gtk.PositionType.RIGHT, 1, 1)
            self.doneBtn = doneBtn

            outScrolledWindow = Gtk.ScrolledWindow()
            outScrolledWindow.set_hexpand(True)
            outScrolledWindow.set_vexpand(True)
            outTextView = Gtk.TextView()
            outTextView.set_property('editable', False)
            outTextView.set_property('cursor-visible', False)
            outScrolledWindow.add(outTextView)
            grid.attach(outScrolledWindow, 0, 4, 3, 1)
            self.outScrolledWindow = outScrolledWindow
            self.outTextView = outTextView
            self.outBuffer = outTextView.get_buffer()
            self.outBuffer.create_mark("end", self.outBuffer.get_end_iter(), False)

            self.open_channel = None

        def scroll_to_bottom(self):
            self.outTextView.scroll_mark_onscreen(self.outBuffer.get_mark("end"))

        def on_download_btn_clicked(self, widget):
            widget.set_sensitive(False)
            self.doneBtn.hide()
            self.outTextView.show()
            init_params = (self.sysOta.get_text(), self.vndOta.get_text(), self.sysType.get_active_text())
            init_runner = threading.Thread(target=self.run_init, args=init_params)
            init_runner.daemon = True
            init_runner.start()

        def run_init(self, systemOta, vendorOta, systemType):
            def draw_sync(s):
                if s.startswith('\r'):
                    last = self.outBuffer.get_iter_at_line(self.outBuffer.get_line_count()-1)
                    last.backward_char()
                    self.outBuffer.delete(last, self.outBuffer.get_end_iter())
                self.outBuffer.insert(self.outBuffer.get_end_iter(), s)
                self.scroll_to_bottom()
            def draw(s):
                GLib.idle_add(draw_sync, s)

            if self.open_channel is not None:
                self.open_channel.close()
                # Wait for other end to re-open
                tmp = helpers.ipc.open_channel("init", "w", buffering=1)
                tmp.close()

            draw("Waiting for waydroid container service...\n")
            try:
                helpers.ipc.notify_blocking(channel="init", msg="{}\f{}\f{}\f{}".format(
                    "cmd", self.sysOta.get_text(), self.vndOta.get_text(), self.sysType.get_active_text()))
            except:
                draw("The waydroid container service is not listening\n")
                GLib.idle_add(self.downloadBtn.set_sensitive, True)
                return

            with helpers.ipc.open_channel("remote_init_output", "rb") as channel:
                self.open_channel = channel
                GLib.idle_add(self.downloadBtn.set_sensitive, True)
                line = ""
                try:
                    while True:
                        data = channel.read(1)
                        if len(data) == 0:
                            draw(line)
                            break
                        c = data.decode()
                        if c == '\r':
                            draw(line)
                            line = c
                        else:
                            line += c
                            if c == '\n':
                                draw(line)
                                line = ""
                except:
                    draw("\nInterrupted\n")

            if is_initialized(args):
                GLib.idle_add(self.doneBtn.show)
                draw("Done\n")


    GLib.set_prgname("Waydroid")
    win = WaydroidInitWindow()
    win.connect("destroy", notify_and_quit)

    win.show_all()
    win.outTextView.hide()
    win.doneBtn.hide()

    Gtk.main()

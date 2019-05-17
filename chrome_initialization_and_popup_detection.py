from uiautomator import Device, JsonRPCError
from subprocess import call, check_output
from timeout_decorator import timeout, TimeoutError
from sys import argv
from time import sleep, time, gmtime
from datetime import datetime
from calendar import timegm
import os
import re
import argparse
import yaml

try:
    from kphs.cloudwatch_metrics_helper import send_or_create_metric_data
    from kphs.cloudwatch_logs_helper import ensure_send_log_stream_data, ensure_log_group_exists
    from kphs.asset_id_retriever import get_asset_id
    from boto import s3
    CLOUDWATCH_IMPORTED = True
    asset_id = get_asset_id()
    generic_cloudwatch_log_prefix = "Asset ID: %s, Message: "%asset_id
    def absolutely_ensure_send_log_stream_data(*args):
        ensure_send_log_stream_data(*args)
except Exception as e:
    print("Couldn't import KPHS packages: ", type(e), e)
    CLOUDWATCH_IMPORTED = False
    generic_cloudwatch_log_prefix = ""
    asset_id = ""
    def absolutely_ensure_send_log_stream_data(*args):
        pass

VERSION_NUMBER = "v1.3"

DEFAULT_PROFILE_NAME = 'default'

DEFAULT_REGION = 'us-west-2'

TIMEOUT_DURATION = 300

class OneTimePopupHandler:

    verbose = False
    output_dir = None
    timeout_duration = 300
    retries = 1
    cloudwatch_metrics = {}
    popup_handling_steps = {}
    log_group_name_and_namespace = "PopupHandler"
    log_stream_name = "Results"
    stage = "gamma"
    start_time_int = timegm(gmtime()) * 1000
    start_time = str(datetime.now())
    

    # d is the uiautomator Device corresponding to this handler
    d = None


    # Create a parser to accept command line arguments.
    def parse_arguments(self):
        parser = argparse.ArgumentParser()
        parser.add_argument("--output-dir", dest="output_dir", type=str, 
            help="output directory for screenshots and screendumps")
        parser.add_argument("--stage", type=str, 
            help="Stage of development out of [gamma, prod]")
        parser.add_argument("-v", "--verbose", action="store_true", dest="verbose",
            help="print debug messages to stdout and write extra screendumps and screenshots")
        parser.add_argument("-t", "--timeout", type=int, dest="timeout_duration", default=300,
            help="timeout for the dismissal in seconds")
        parser.add_argument("-r", "--retries", type=int, default=1,
            help="retries on performing walkthrough")
        args = parser.parse_args(argv[1:])
        for arg in vars(args):
            setattr(self, arg, getattr(args, arg))

    # Initialize the UIAutomator device
    def initialize_device(self, retry=True):
        try:
            self.d = Device()
            self.d.orientation = "n"
            self.d.press.home()
        except JsonRPCError as e:
            if retry:
                self.initialize_device(retry=False)
            else:
                #print and metrics
                self.cloudwatch_metrics["Errored"] = 1
                raise e

    # Create a uiautomator Device when we create a OneTimePopupHandler object
    def __init__(self):
        self.initialize_device()
        if self.verbose:
            self.dump_screen_information("starting_screen")
        if CLOUDWATCH_IMPORTED:
            ensure_log_group_exists(self.log_group_name_and_namespace)

    # Delete the uiautomator Device when we delete a OneTimePopupHandler object,
    # and uninstall any added packages from the device
    def __del__(self):
        if self.verbose:
            self.dump_screen_information("ending_screen")
        self.d = None
        for command in [["adb", "uninstall", "com.github.uiautomator"],
            ["adb", "uninstall", "com.github.uiautomator.test"],
            ["adb", "shell", "am", "start", "-a", "android.intent.action.MAIN",
                "-c", "android.intent.category.HOME"]]:
            try:
                call(command)
            except Exception:
                pass
        class ExtraSpacingDumper(yaml.SafeDumper):

            def increase_indent(self, flow=False, indentless=True):
                return super(ExtraSpacingDumper, self).increase_indent(flow, False)

        if self.popup_handling_steps and self.output_dir:
            with open(os.path.join(self.output_dir, "popups_dismissed.yml"), "w") as s:
                yaml.dump(self.popup_handling_steps,
                    Dumper=ExtraSpacingDumper, default_flow_style=False,
                    width=200, stream=s)

        call(["chmod", "-R", "777", self.output_dir])
        if CLOUDWATCH_IMPORTED:
            if self.popup_handling_steps:
                self.cloudwatch_metrics["ObservedPopups"] = len(set(self.popup_handling_steps).union(
                    set([x.split("Failed_")[1] for x in self.cloudwatch_metrics if "Failed_" in x])))
                self.cloudwatch_metrics["DismissedPopups"] = self.cloudwatch_metrics["ObservedPopups"] - len(
                    [x.split("Failed_")[1] for x in self.cloudwatch_metrics if "Failed_" in x])
            else:
                self.cloudwatch_metrics["DismissedPopups"] = 0
                self.cloudwatch_metrics["ObservedPopups"] = 0
            for key, value in self.cloudwatch_metrics.items():
                send_or_create_metric_data(self.log_group_name_and_namespace, key, value)
            self.upload_logs()


    def dump_screen_information(self, name, dump=None):
        """
        Method to capture a screenshot and dump the screen hierarchy of elements, saving these things
        to the previously-specified output_dir


        Args:
            name (str): The name of the screenshot at name.png and the screendump at name_screendump.txt
            dump (str): A optional previous screendump already captured and saved (as an optimization)
        """
        if self.output_dir:
            if not os.path.isdir(self.output_dir):
                call(["mkdir", "-p", self.output_dir])
            if not dump:
                dump = self.d.dump()
            with open(os.path.join(self.output_dir, name+"_screendump.txt"), "w") as f:
                f.write(dump.encode("ascii", "ignore"))
            call(["adb", "shell", "screencap", "-p", "/sdcard/%s.png"%name])
            call(["adb", "pull", "/sdcard/%s.png"%name])
            call(["mv", name+".png", self.output_dir])
            call(["adb", "shell", "rm", "/sdcard/%s.png"%name])

    def upload_logs(self, profile_name=DEFAULT_PROFILE_NAME, region=DEFAULT_REGION):
        '''
        Upload all produced files to S3 in the popup-logs folder
        '''
        client = s3.connect_to_region(region, profile_name=profile_name)
        popup_logs_bucket = client.get_bucket('{}-{}-harness-popup-logs'.format(self.stage, region))
        
        for filename in os.listdir(self.output_dir):
            with open(os.path.join(self.output_dir, filename)) as f:
                print('Uploading {} to S3'.format(filename))
                key = popup_logs_bucket.new_key('/'.join([
                    asset_id, self.start_time.replace(' ','_'), filename]))
                key.set_contents_from_file(f)
                print('Finished uploading {} to S3'.format(filename))

    def save_popup_walkthrough(self, stage, step):
        """
        Save a step in your walkthrough of dismissing some stage of popups so it can be logged later
        """
        if stage not in self.popup_handling_steps:
            self.popup_handling_steps[stage] = []
        identifiers = {}
        for identifier in ["className", "text", "contentDescription", "resourceName", "packageName", "checked"]:
            if identifier in step and step[identifier]:
                identifiers[identifier] = step[identifier]
        self.popup_handling_steps[stage].append(identifiers)
        if not identifiers:
            print("Couldnt find any identifiers in %s"%step)

    def perform_popup_step(self, name, step):
        self.save_popup_walkthrough(name, step.info)
        step.click.wait()

    def dismiss_any_sporadic_popups(self):
        """
        This is a direct python conversion of our TestAndroidPopUps.java file, ment to accomplish the same task.
        """
        if self.verbose:
            print("Handling initial sporadic popups")
            self.dump_screen_information("handling_sporadic_popups")
        popup_selectors = {
            "safeSimSelector": {"textMatches": ".*(?i)\\b(sim|mobile data)\\b.*"},
            "unfortunatelySelector": {"textStartsWith": "Unfortunately"},
            "notRespondingSelector": {"textContains": "responding"},
            "safeWhitelistSelector": {"textMatches": ".*(?i)\\b(attention|hands free activation|multi window|select home|update firmware)\\b.*"},
            "negatorySelector": {"clickable": True, "textMatches": ".*(?i)\\b(cancel|later|no|deny|decline|skip|close app|don't send|block|just once)\\b.*"},
            "affirmatorySelector": {"clickable": True, "textMatches": ".*(?i)\\b(ok|okay|yes|start|accept|allow)\\b.*"},
            "affirmatorySelectorFalsePositive": {"clickable": True, "textMatches": ".*(?i)\\b(autostart)\\b.*"},
            "softwareUpdateSelector": {"textMatches": 
                ".*(?i)\\b(install overnight|download|yes, i'm in|install|install now|software update|software upgrade|system upgrade|system update|system software)\\b.*"},
            "doNotShowAgainSelector": {"clickable": True, "textMatches": "(?i)(do not|don't) show again"},
        }
        def dont_show_again():
            if (self.d(**popup_selectors["doNotShowAgainSelector"]).exists and
                self.d(**popup_selectors["doNotShowAgainSelector"]).checked):
                self.d(**popup_selectors["doNotShowAgainSelector"]).click.wait()
        i = 0
        while i < 5:
            if self.d(**popup_selectors["softwareUpdateSelector"]).exists:
                self.dump_screen_information("detected_system_update")
                if self.verbose:
                    print("Found a system update popup")
                return False
            if self.d(**popup_selectors["negatorySelector"]).exists:
                dont_show_again()
                self.d(**popup_selectors["negatorySelector"]).click.wait()
                i+=1
            if self.d(**popup_selectors["affirmatorySelector"]).exists:
                if (self.d(**popup_selectors["safeSimSelector"]).exists or
                    self.d(**popup_selectors["unfortunatelySelector"]).exists or
                    self.d(**popup_selectors["notRespondingSelector"]).exists or
                    self.d(**popup_selectors["safeWhitelistSelector"]).exists):
                    dont_show_again()
                    self.d(**popup_selectors["affirmatorySelector"]).click.wait()
                elif self.d(**popup_selectors["affirmatorySelectorFalsePositive"]).exists:
                    return True
                else:
                    self.d.press.back()
                i+=1
            else:
                if self.verbose:
                    print("Handled initial sporadic popups")
                    self.dump_screen_information("handled_sporadic_popups")
                return True
        print("Failed to handle initial sporadic popups")
        self.dump_screen_information("failed_to_dismiss_sporadic_popups")
        return False

    def trigger_and_handle_app_switch_popup(self):
        """
        Triggers and handles the popup explaining how the app switcher works
        Uses screen dumps as an optimization in some cases instead of selector calls
        """
        self.d.press(0xbb)
        sleep(3)
        if self.verbose:
            print("Handling app switcher initial prompts")
            self.dump_screen_information("handling_app_switcher_prompts")
        for i in range(4):
            screen_dump = self.d.dump()
            if ((screen_dump.find('class="android.widget.CheckBox"') != -1) and
                self.d(className="android.widget.CheckBox", textMatches=".*(?i)\\b(do not).*").exists and
                not self.d(className="android.widget.CheckBox", textMatches=".*(?i)\\b(do not).*").checked):
                self.perform_popup_step("app_switcher_popups", self.d(className="android.widget.CheckBox", textContains="Do not"))
            if re.search('text=".*(?i)\\b(ok).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(ok).*").exists:
                self.perform_popup_step("app_switcher_popups", self.d(textMatches=".*(?i)\\b(ok).*"))
            elif re.search('text=".*(?i)\\b(next).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(next).*").exists:
                self.perform_popup_step("app_switcher_popups", self.d(textMatches=".*(?i)\\b(next).*"))
            else:
                break
        if re.search('text=".*(?i)\\b(close).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(close).*").exists:
            self.d(textMatches=".*(?i)\\b(close).*").click()
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(clear).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(clear).*").exists:
            self.d(textMatches=".*(?i)\\b(clear).*").click()
            screen_dump = self.d.dump()
        if self.verbose:
            print("Handled app switcher initial prompts")
            self.dump_screen_information("handled_app_switcher_prompts", dump=screen_dump)
        success=False
        if not re.search('text=".*(?i)\\b(ok).*"', screen_dump) and not re.search('text=".*(?i)\\b(next).*"', screen_dump):
            self.cloudwatch_metrics["Passed_app_switcher_popups"] = 1
            if self.verbose:
                print("Handled app switcher popups just fine")
            success=True
        else:
            print("Failed to handled app switcher initial prompts")
            self.cloudwatch_metrics["Failed_app_switcher_popups"] = 1
            absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                self.log_stream_name, self.start_time_int,
                self.start_time+" - "+generic_cloudwatch_log_prefix
                +"Failed to dismiss app switch popups")
            self.dump_screen_information("failed_to_handle_app_switcher_prompts", dump=screen_dump)
        self.d.press.back()
        self.d.press.back()
        return success

    def trigger_and_handle_camera_popups(self):
        """
        Triggers and handles the popup explaining how the camera works
        """
        if self.verbose:
            print("Handling camera initial prompts")
            self.dump_screen_information("handling_camera_prompts")
        call(["adb", "shell", "am", "start", "-a",
            "android.media.action.IMAGE_CAPTURE"])
        sleep(1)
        self.dismiss_any_sporadic_popups()
        if self.d(className="android.widget.Button", textMatches=".*(?i)\\b(ok).*").exists:
            self.perform_popup_step("camera_prompts", self.d(className="android.widget.Button", textMatches=".*(?i)\\b(ok).*"))
        if self.d(textMatches=".*(?i)\\b(next).*").exists:
            self.perform_popup_step("camera_prompts", self.d(textMatches=".*(?i)\\b(next).*"))
            if self.d(textMatches=".*(?i)\\b(next).*").exists:
                self.perform_popup_step("camera_prompts", self.d(textMatches=".*(?i)\\b(next).*"))
            if self.d(textMatches=".*(?i)\\b(ok).*").exists:
                self.perform_popup_step("camera_prompts", self.d(textMatches=".*(?i)\\b(ok).*"))
            elif self.d(textMatches=".*(?i)\\b(done).*").exists:
                self.perform_popup_step("camera_prompts", self.d(textMatches=".*(?i)\\b(done).*"))
        if self.d(textMatches=".*(?i)\\b(ok).*").exists:
            self.perform_popup_step("camera_prompts", self.d(textMatches=".*(?i)\\b(ok).*"))
        if self.verbose:
            print("Handled camera initial prompts")
            self.dump_screen_information("handled_camera_prompts")
        if (not self.d(textMatches=".*(?i)\\b(ok).*").exists and
            not self.d(textMatches=".*(?i)\\b(next).*").exists and
            not self.d(textMatches=".*(?i)\\b(done).*").exists):
            self.cloudwatch_metrics["Passed_camera_prompts"] = 1
            if self.verbose:
                print("Handled camera prompts just fine")
            return True
        else:
            print("Failed to handled camera prompts")
            self.cloudwatch_metrics["Failed_camera_prompts"] = 1
            self.dump_screen_information("failed_to_handle_camera_prompts")
            absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                self.log_stream_name, self.start_time_int,
                self.start_time+" - "+generic_cloudwatch_log_prefix
                +"Failed to dismiss camera popups")
            return False

    def handle_initial_popups(self):
        """
        Triggers and handles the "initial" popups explaining how full-screen apps and multiwindow work
        Uses screen dumps as an optimization in some cases instead of selector calls
        """
        screen_dump = self.d.dump()
        if self.verbose:
            print("Handling general app initial prompts")
            self.dump_screen_information("handling_app_initial_prompts", dump=screen_dump)
        i = 0
        while i < 3 and not ((screen_dump.find('resource-id="com.android.chrome:id/menu_button"') != -1) or
            (screen_dump.find('resource-id="com.android.chrome:id/tab_switcher_button"') != -1) or
            (screen_dump.find('text="Search or type web address"') != -1) or
            (screen_dump.find('class="android.widget.CheckBox"') != -1)):
            sleep(1)
            i+=1
            screen_dump = self.d.dump()
        if ((screen_dump.find('class="android.widget.CheckBox"') != -1) and not
            self.d(className="android.widget.CheckBox").checked):
            self.perform_popup_step("initial_popups", self.d(className="android.widget.CheckBox"))
        if re.search('text=".*(?i)\\b(ok).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(ok).*").exists:
            self.perform_popup_step("initial_popups", self.d(textMatches=".*(?i)\\b(ok).*"))
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(next).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(next).*").exists:
            self.perform_popup_step("initial_popups", self.d(textMatches=".*(?i)\\b(next).*"))
            if self.d(textMatches=".*(?i)\\b(ok).*").exists:
                self.perform_popup_step("initial_popups", self.d(textMatches=".*(?i)\\b(ok).*"))
            screen_dump = self.d.dump()
        if self.verbose:
            print("Handled general app initial prompts")
            self.dump_screen_information("handled_app_initial_prompts", dump=screen_dump)
        if not re.search('text=".*(?i)\\b(ok).*"', screen_dump) and not re.search('text=".*(?i)\\b(next).*"', screen_dump):
            self.cloudwatch_metrics["Passed_initial_popups"] = 1
            if self.verbose:
                print("Handled camera prompts just fine")
            return True
        else:
            print("Failed to handled initial prompts")
            self.cloudwatch_metrics["Failed_initial_popups"] = 1
            self.dump_screen_information("failed_to_handle_initial_popups", dump=screen_dump)
            absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                self.log_stream_name, self.start_time_int,
                self.start_time+" - "+generic_cloudwatch_log_prefix
                +"Failed to dismiss intiial popups")
            return False

    def handle_initial_chrome_prompts(self):
        """
        Handles the popups explaining the google chrome ToS
        Uses screen dumps as an optimization in some cases instead of selector calls
        """
        screen_dump = self.d.dump()
        if self.verbose:
            print("Handling Chrome initial prompts")
            self.dump_screen_information("starting_chrome_initial_prompts", dump=screen_dump)
        if ((screen_dump.find('class="android.widget.CheckBox"') != -1) and
            self.d(className="android.widget.CheckBox").checked):
            self.perform_popup_step("initial_chrome_prompts", self.d(className="android.widget.CheckBox"))
        if re.search('text=".*(?i)\\b(undo).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(undo).*").exists:
            self.perform_popup_step("initial_chrome_prompts", self.d(textMatches=".*(?i)\\b(undo).*"))
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(accept).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(accept).*").exists:
            self.perform_popup_step("initial_chrome_prompts", self.d(textMatches=".*(?i)\\b(accept).*"))
            sleep(3)
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(no)\\b.*"', screen_dump) and self.d(textMatches=".*(?i)\\b(no)\\b.*").exists:
            self.perform_popup_step("initial_chrome_prompts", self.d(textMatches=".*(?i)\\b(no)\\b.*"))
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(continue).*"', screen_dump) and self.d(textMatches=".*(?i)\\b(continue).*").exists:
            self.perform_popup_step("initial_chrome_prompts", self.d(textMatches=".*(?i)\\b(continue).*"))
            sleep(1)
            screen_dump = self.d.dump()
        if re.search('text=".*(?i)\\b(no)\\b.*"', screen_dump) and self.d(textMatches=".*(?i)\\b(no)\\b.*").exists:
            self.perform_popup_step("initial_chrome_prompts", self.d(textMatches=".*(?i)\\b(no)\\b.*"))
            screen_dump = self.d.dump()
        if self.verbose:
            print("Handled Chrome initial prompts")
            self.dump_screen_information("finishing_chrome_initial_prompts", dump=screen_dump)

        text_box = self.find_text_box()
        if text_box.exists:
            self.cloudwatch_metrics["Passed_initial_chrome_prompts"] = 1
            if self.verbose:
                print("Handled initial chrome popups just fine")
            return True
        else:
            print("Failed to handled chrome initial prompts because a text box isnt there")
            self.cloudwatch_metrics["Failed_initial_chrome_prompts"] = 1
            self.dump_screen_information("failed_to_handle_initial_chrome_prompts")
            absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                self.log_stream_name, self.start_time_int,
                self.start_time+" - "+generic_cloudwatch_log_prefix
                +"Failed to dismiss intitial chrome popups because there's no text box in chrome")
            return False

    def find_text_box(self):
        """
        Find and return a text box for the text box popup handler
        """
        if self.verbose:
            print("Finding a text box")
            self.dump_screen_information("finding_text_box")
        text_box = self.d(className="android.widget.EditText")
        if not text_box.exists:
            if self.verbose:
                print("Restarting Chrome and scrolling up to try and find one")
            call(["adb", "shell", "am", "start", "-n",
                "com.android.chrome/com.google.android.apps.chrome.Main"])
            i=0
            while i < 3 and not text_box.exists:
                self.d().swipe.up()
                i+=1
                text_box = self.d(className="android.widget.EditText")
        if self.verbose:
            print("Finished finding a text box")
            self.dump_screen_information("finished_finding_text_box")
        return text_box

    def check_for_keyboard_tips(self):
        """
        Handles the popups explaining how the keyboard works via some helpful "tips"
        """
        if self.d(className="android.widget.CheckBox", textContains="Do not").exists:
            if self.verbose:
                print("Handling keyboard tips popup dialog")
                self.dump_screen_information("handling_keyboard_tips")
            if (self.d(className="android.widget.CheckBox", textContains="Do not").exists and not
                self.d(className="android.widget.CheckBox", textContains="Do not").checked):
                self.perform_popup_step("text_popups", self.d(className="android.widget.CheckBox", textContains="Do not"))
            if self.d(className="android.widget.Button", textContains="Next").exists:
                self.perform_popup_step("text_popups", self.d(className="android.widget.Button", textContains="Next"))
            if (self.d(className="android.widget.CheckBox", textContains="Do not").exists and not
                self.d(className="android.widget.CheckBox", textContains="Do not").checked):
                self.perform_popup_step("text_popups", self.d(className="android.widget.CheckBox", textContains="Do not"))
            if self.d(className="android.widget.Button", textContains="Dismiss").exists:
                self.perform_popup_step("text_popups", self.d(className="android.widget.Button", textContains="Dismiss"))
            if self.verbose:
                print("Handled keyboard tips popup dialog")
                self.dump_screen_information("handled_keyboard_tips")

    def handle_keyboard_settings(self):
        """
        Goes to the keyboard settings and disables any predictive text analytics stuff
        Uses screen dumps as an optimization in some cases instead of selector calls
        """
        if self.d(text="Settings").exists:
            self.perform_popup_step("text_popups", self.d(text="Settings"))
            screen_dump = self.d.dump()
            if self.verbose:
                print("Disabling keyboard predictive settings")
                self.dump_screen_information("handling_predictive_settings", dump=screen_dump)
            i = 0
            while i < 10 and not (re.search('text=".*(?i)\\b(Personalized).*"', screen_dump) or
                re.search('text=".*(?i)\\b(personal language).*"', screen_dump) or
                re.search('text=".*(?i)\\b(Predictive).*"', screen_dump)):
                sleep(1)
                i+=1
                screen_dump = self.d.dump()
            if (re.search('text=".*(?i)\\b(Personalized).*"', screen_dump) and 
                self.d(textContains="Personalized",).exists and
                self.d(textContains="Personalized",).right(className="android.widget.CheckBox") and
                self.d(textContains="Personalized",).right(className="android.widget.CheckBox").checked):
                self.perform_popup_step("text_popups",
                    self.d(textContains="Personalized",).right(className="android.widget.CheckBox"))
            if (re.search('text=".*(?i)\\b(personal language).*"', screen_dump) and 
                self.d(textContains="personal language",).exists and 
                self.d(textContains="personal language",).right(className="android.widget.CheckBox") and
                self.d(textContains="personal language",).right(className="android.widget.CheckBox").checked):
                self.perform_popup_step("text_popups",
                    self.d(textContains="personal language",).right(className="android.widget.CheckBox"))
            if (re.search('text=".*(?i)\\b(Personalized).*"', screen_dump) and
                self.d(textContains="Personalized",className="android.widget.CheckBox").exists and
                self.d(textContains="Personalized",className="android.widget.CheckBox").checked):
                self.perform_popup_step("text_popups", 
                    self.d(textContains="Personalized",className="android.widget.CheckBox"))
            if (re.search('text=".*(?i)\\b(Predictive).*"', screen_dump) and
                self.d(textContains="Predictive", resourceId="android:id/action_bar_title").exists and 
                self.d(textContains="Predictive", resourceId="android:id/action_bar_title").right(
                    className="android.widget.Switch") and
                self.d(textContains="Predictive", resourceId="android:id/action_bar_title").right(
                    className="android.widget.Switch").checked):
                self.perform_popup_step("text_popups", 
                    self.d(textContains="Predictive", resourceId="android:id/action_bar_title").right(
                        className="android.widget.Switch"))
            if self.verbose:
                print("Disabled keyboard predictive settings")
                self.dump_screen_information("handled_predictive_settings", dump=screen_dump)
            if (re.search('text=".*(?i)\\b(Personalized).*"', screen_dump) or
                re.search('text=".*(?i)\\b(personal language).*"', screen_dump) or
                re.search('text=".*(?i)\\b(Predictive).*"', screen_dump)):
                self.popup_handling_steps["text_popups"].append({"press": "back"})
                self.d.press.back()
                sleep(2)
                if self.d(className="android.widget.EditText").exists:
                    self.perform_popup_step("text_popups", 
                        self.d(className="android.widget.EditText"))

    def handle_text_popups(self, retry=True):
        """
        Handles any popups prompted by text boxes
        """
        try:
            if retry and self.verbose:
                print("Handling text box popups")
                self.dump_screen_information("handling_text_box_popups",)
            self.d(className="android.widget.EditText").click.wait()
            sleep(1)
            if self.d(className="android.widget.CheckBox").exists and self.d(className="android.widget.CheckBox").checked:
                self.perform_popup_step("text_popups", self.d(className="android.widget.CheckBox"))
            self.check_for_keyboard_tips()
            self.handle_keyboard_settings()
            self.check_for_keyboard_tips()
            if self.d(text="No, thanks").exists:
                self.perform_popup_step("text_popups", self.d(text="No, thanks"))
            if self.d(text="No").exists:
                self.perform_popup_step("text_popups", self.d(text="No"))
            if self.d(text="OK").exists:
                self.perform_popup_step("text_popups", self.d(text="OK"))
                sleep(2)
                if self.d(className="android.widget.EditText").exists:
                    self.perform_popup_step("text_popups", self.d(className="android.widget.EditText"))
            if (self.d(className="android.widget.CheckBox", textContains="Do not").exists and not
                self.d(className="android.widget.CheckBox", textContains="Do not").checked):
                self.perform_popup_step("text_popups", 
                    self.d(className="android.widget.CheckBox", textContains="Do not"))
                if self.d(text="OK").exists:
                    self.perform_popup_step("text_popups", self.d(text="OK"))
            if (self.d(className="android.widget.CheckBox", textContains="Turn on personalized").exists and
                self.d(className="android.widget.CheckBox", textContains="Turn on personalized").checked):
                self.perform_popup_step("text_popups", 
                    self.d(className="android.widget.CheckBox", textContains="Turn on personalized"))
            elif (self.d(className="android.widget.CheckBox", textContains="personalized").exists and not
                self.d(className="android.widget.CheckBox", textContains="personalized").checked):
                self.perform_popup_step("text_popups", 
                    self.d(className="android.widget.CheckBox", textContains="personalized"))
            if self.d(text="OK").exists:
                self.perform_popup_step("text_popups", self.d(text="OK"))
            if self.d(text="A picture is worth 1000 words").exists:
                self.perform_popup_step("text_popups", self.d(text="NEXT"))
                sleep(2)
            if self.d(text="START").exists:
                self.perform_popup_step("text_popups", self.d(text="START"))
                sleep(2)
            self.handle_keyboard_settings()
            if retry and self.verbose:
                print("Handled text box popups")
                self.dump_screen_information("handling_text_box_popups",)
        except Exception as e:
            print(type(e), e, e.message if "message" in e.__dict__ else "")
            if type(e) is TimeoutError:
                raise e
            if retry:
                self.d = None
                self.d = Device()
                if self.verbose:
                    print("Handling text box popups attempt 2")
                    self.dump_screen_information("handling_text_box_popups_retry",)
                return self.handle_text_popups(retry=False)
        return self.check_if_text_popups_dismissed()

    def check_if_text_popups_dismissed(self):
        """
        Checks if we can enter text in a text box
        """
        if self.verbose:
            print("Trying to enter text in a textbox")
            self.dump_screen_information("attempting_to_enter_text")
        retries_left = 1
        while retries_left >= 0 and not self.d(textContains="Hello!").exists:
            retries_left -= 1
            try:
                text_box = self.d(className="android.widget.EditText")
                text_box.click()
                text_box.clear_text()
                text_box.set_text("Hello!")
                i = 0
                while i < 3 and not self.d(textContains="Hello!").exists:
                    sleep(1)
                    i+=1
            except Exception as e:
                print("Failed to click text box due to an error:", type(e), e,
                    e.message if "message" in e.__dict__ else "")

        if not self.d(textContains="Hello!").exists:
            print("Failed to enter text because the popup prevented us")
            self.dump_screen_information("failed_to_dismiss_text_popup")
            self.cloudwatch_metrics["Failed_textbox_popups"] = 1
            absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                self.log_stream_name, self.start_time_int,
                self.start_time+" - "+generic_cloudwatch_log_prefix
                +"Failed to dismiss popups despite trying to enter text into the textbox.")
            return False
        if self.verbose:
            print("Entered the text just fine")
        self.cloudwatch_metrics["Passed_textbox_popups"] = 1
        self.d(textContains="Hello!").clear_text()
        self.d(className="android.widget.EditText").clear_text()
        return True

    @timeout(TIMEOUT_DURATION)
    def perform_popup_walkthrough(self):
        success = False
        attempts = 0
        while attempts < self.retries and not success:
            if CLOUDWATCH_IMPORTED:
                self.cloudwatch_metrics = {}
            try:
                success = self.popup_walkthrough()
            except Exception as e:
                print("Failed due to an error:", type(e), e, e.message if "message" in e.__dict__ else "")
            attempts+=1
        if success:
            self.cloudwatch_metrics["Passed"] = 1
        elif "Errored" not in self.cloudwatch_metrics and "TimedOut" not in self.cloudwatch_metrics:
            self.cloudwatch_metrics["Failed"] = 1

        return success


    def popup_walkthrough(self):
        '''
        Perform a walkthrough of the steps to initiate and dismiss many one-time popups. This includes the
        following popups (in general order of appearence):
            - The app drawer explaination
            - The multi window tray explaination
            - Pop-up view explaination
            - Keyboard layout popup
            - Samsung keyboard tips
            - Samsung keyboard predictive results
            - LG keyboard predictive data
            - Samsung "a picture is worth 1000 words" GIF keyboard explaination
        '''
        try:
            if not self.dismiss_any_sporadic_popups():
                self.cloudwatch_metrics["CouldntStartTest"] = 1
                absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                    self.log_stream_name, self.start_time_int,
                    self.start_time+" - "+generic_cloudwatch_log_prefix
                    +"Failed to initialize test due to sporadic popups like system updates")
                return False

            if self.verbose:
                print("=================LISTING PACKAGES======================")
                print(str(check_output(["adb", "shell", "cmd", "package", "list", "packages"]).encode("utf-8")))

            #clear all other keyboards
            if self.verbose:
                print("Clearing out latin keyboard:")
            call(["adb", "shell", "pm", "clear", "com.google.android.inputmethod.latin"])
            sleep(2)

            #Clear hindi keyboard
            if self.verbose:
                print("Clearing out hindi keyboard:")
            call(["adb", "shell", "pm", "clear", "com.google.android.apps.inputmethod.hindi"])
            sleep(2)

            # Clear samsung keyboard
            if self.verbose:
                print("Clearing out samsung keyboard:")
            call(["adb", "shell", "pm", "clear", "com.sec.android.inputmethod"])
            sleep(2)

            call(["adb", "shell", "am", "start", "-n",
                "com.android.chrome/com.google.android.apps.chrome.Main"])
            sleep(1)

            success = self.handle_initial_popups()

            #these depend on the previous step
            if success:
                success = self.handle_initial_chrome_prompts()

            #these depend on the previous step
            if success:
                success = self.handle_text_popups()
                self.d.press.back()
                self.d.press.back()
                self.d.press.back()

            success = success and self.trigger_and_handle_camera_popups()
            
            success = success and self.trigger_and_handle_app_switch_popup()

            return success

        except Exception as e:
            try:
                print("Failed due to an error:", type(e), e, e.message if "message" in e.__dict__ else "")
                self.cloudwatch_metrics["Errored"] = 1
                absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                    self.log_stream_name, self.start_time_int,
                    self.start_time+" - "+generic_cloudwatch_log_prefix
                    +"Failed due to an error: %s, %s, %s."
                    %(type(e), e, e.message if "message" in e.__dict__ else ""))
                self.dump_screen_information("failed_due_to_python_error_"+str(e).strip('()'))
            except Exception as ee:
                print("Ran into another error while handling the first error",
                    type(ee), ee, ee.message if "message" in ee.__dict__ else "")
            if type(e) is TimeoutError:
                self.cloudwatch_metrics["TimedOut"] = 1
                absolutely_ensure_send_log_stream_data(self.log_group_name_and_namespace,
                    self.log_stream_name, self.start_time_int,
                    self.start_time+" - "+generic_cloudwatch_log_prefix
                    +"Failed due to a timeout.")
                raise e

        return False


if __name__ == "__main__":
    start_time = time()
    print(("Starting one-time popup handler %s! Walking through some "%VERSION_NUMBER)
        +"standard UI interactions, and dismissing popups along the way")
    success = False
    duration = -1
    try:
        popup_handler_class = OneTimePopupHandler()
        popup_handler_class.parse_arguments()
        TIMEOUT_DURATION = popup_handler_class.timeout_duration
        try:
            success = popup_handler_class.perform_popup_walkthrough()
        except TimeoutError:
            print("Timed out while trying to walk through popups")
            success = False
            popup_handler_class.dump_screen_information("timed_out_at_this_point")
        duration = time()-start_time
        popup_handler_class.cloudwatch_metrics["Duration"] = duration
        popup_handler_class = None
    except Exception as e:
        print(type(e), e, e.message if "message" in e.__dict__ else "")
    print("Took %s to %s complete the popup dismissal walkthrough"%
        (duration, "successfully" if success else "unsuccessfully"))
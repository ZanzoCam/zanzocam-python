import sys
import json
import logging
import datetime
from time import sleep

from zanzocam.constants import (
    UPLOAD_LOGS,
    CAMERA_LOG,
    WAIT_AFTER_CAMERA_FAIL
)
from zanzocam.webcam import system
from zanzocam.webcam import configuration
from zanzocam.webcam.server import Server
from zanzocam.webcam.camera import Camera
from zanzocam.webcam.errors import ServerError
from zanzocam.webcam.utils import log, log_error, log_row


def main():
    """
    Main script coordinating all operations.
    """
    # Setup the logging
    logging.basicConfig(
        level=logging.INFO,
        format='%(message)s',
        handlers=[
            logging.FileHandler(CAMERA_LOG),
            logging.StreamHandler(sys.stdout),
        ]
    )
    log_row()
    log("Start")

    try:
        upload_logs = UPLOAD_LOGS
        restore_required = False
        config = None
        server = None
        camera = None

        start = datetime.datetime.now()

        # System check
        no_errors = system.log_general_status()
 
        # Locale setup
        no_errors = system.set_locale()
 
        # Load the configuration from disk
        config = configuration.load_configuration_from_disk()
        if not config:
            log_error("", fatal="cannot proceed without any data. Exiting.")
            no_errors = False
            upload_logs = False
            return

        # Make sure configuration and system status match
        no_errors = system.apply_system_settings(config.get_system_settings())

        # Check active time
        is_active_time = config.within_active_hours()
        if is_active_time is False:
            log("Turning off.")
            return
        if is_active_time is None:  # In case of errors
            log_error("Continuing the run.")
            no_errors = True

        # Create the server
        server = Server(config.get_server_settings())

        # Update the configuration file
        new_config = server.update_configuration(config)
        if new_config:
        
            # Update the system to conform to the new configuration file
            no_errors = system.apply_system_settings(new_config.get_system_settings())
            config = new_config

        log(f"Configuration in use:\n{config}")

        # Recreate the server (might differ in the new configuration)
        server = Server(config.get_server_settings())

        # Download the overlays
        overlays_list = config.list_overlays()
        no_errors = server.download_overlay_images(overlays_list)

        # Take the picture
        for _ in range(3):
            log("Initializing camera...")
            try:
                camera = Camera(config.get_camera_settings())
                camera.take_picture()
                break

            except Exception as e:
                log_error("An exception occurred!", e)
                log(f"Waiting for {WAIT_AFTER_CAMERA_FAIL} sec. "
                    "and retrying...")
                sleep(WAIT_AFTER_CAMERA_FAIL)

        if not camera:
            no_errors = False
            return

        # Send the picture
        no_errors = server.upload_picture(camera.processed_image_path,
                                          camera.name,
                                          camera.extension)

        # Cleanup the image files
        no_errors = camera.cleanup_image_files()


    # Catch server errors: they block communication, so they are fatal anyway
    except Exception as e:
        no_errors = False

        if isinstance(e, ServerError):
            log_error("An error occurred communicating with the server.",
                      e, fatal="Restoring the old configuration and exiting.")
        else:
            log_error("Something unexpected occurred running the main procedure.",
                      e, fatal="Restoring the old configuration and exiting.")

        log("Restoring the old configuration file. Note that this "
                "operation affects the server settings only: system "
                "settings might be still setup according to the newly "
                "downloaded configuration file (if it was downloaded). "
                "Check the above logs carefully to assess the situation.")
 
        if config:
            try:
                no_errors = config.restore_backup()
                old_config = configuration.load_configuration_from_disk()
                server_config = json.dumps(old_config.get_server_settings(), indent=4)
                log(f"The next run will use the following server "
                    f"configuration:\n{server_config}")

            except Exception as e:
                log_error("Failed to restore the backup config. "
                          "ZanzoCam might have no valid config file for the next run.")

    
    # This block is called even after a return
    finally:

        errors_str = "successfully"
        if not no_errors or restore_required:
            errors_str = "with errors"

        end = datetime.datetime.now()
        log(f"Execution completed {errors_str} in: {end - start}")
        log_row()


        # Upload the logs
        if upload_logs:
            try:
                current_conf = configuration.load_configuration_from_disk()
                server = Server(current_conf.get_server_settings())
                server.upload_logs()
            except Exception as e:
                log_error("Something went wrong uploading the logs. "
                          "Logs won't be uploaded.")


if "__main__" == __name__:
    main()

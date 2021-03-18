from typing import Any, Dict, Tuple, Optional

import os
import math
import datetime
import textwrap
from time import sleep
from pathlib import Path
from picamera import PiCamera
from PIL import Image, ImageFont, ImageDraw, ImageStat

from webcam.constants import *
from webcam.utils import log, log_error
from webcam.configuration import Configuration 


# Minimum luminance for the daytime. 
# If the detected luminance goes below this value, the night mode kicks in.
MINIMUM_DAYLIGHT_LUMINANCE=60


# Default parameters for the luminance/shutterspeed interpolation curve.
# Calculated for a Raspberry Pi Camera v1.2
# They can be overridden by custom calibrated parameters.
LUM_SPEED_PARAM_A = 600000
LUM_SPEED_PARAM_B = 60000


# luminance/shutterspeed interpolation extremes for the shutter speed
MIN_SHUTTER_SPEED = int(0.03 * 10**6)
MAX_SHUTTER_SPEED = int(3 * 10**6)
TARGET_LUMINOSITY_MARGIN = 1

# Calibration results output table
CALIBRATION_CSV = Path(__file__).parent.parent / "luminance_speed_table.csv"
CALIBRATION_FLAG = Path(__file__).parent.parent / "CALIBRATION"
CALIBRATED_PARAMETERS = Path(__file__).parent.parent / "CALIBRATED_PARAMS"


# Given a low luminance value, return the shutter speed required to raise the 
# final luminance to about 30
SHUTTER_SPEED_FROM_LUMINANCE = \
    lambda lum, a, b: int(((a/lum) + b)) if lum < MINIMUM_DAYLIGHT_LUMINANCE else None

# Given the RGB values of a picture (ImageStat.Stat(photo).mean)
# returns its luminance
LUMINANCE_FROM_RGB = \
    lambda r,g,b: math.sqrt(0.241*(r**2) + 0.691*(g**2) + 0.068*(b**2))

# Given a PIL picture, returns its luminance
LUMINANCE_FROM_PICTURE = \
    lambda photo: LUMINANCE_FROM_RGB(*ImageStat.Stat(photo).mean)

# Given a luminance < MINIMUM_DAYLIGHT_LUMINANCE, 
# calculate an appropriate luminance value to raise the image to
TARGET_LUMINANCE = \
    lambda lum: lum if lum > MINIMUM_DAYLIGHT_LUMINANCE else lum+30 if lum < 30 else (lum/2) + 45

# Given a picture with low luminosity (< MINIMUM_DAYLIGHT_LUMINANCE)
# returns the appropriate shutter speed to acheve a good target luminosity
SHUTTER_SPEED_FROM_PICTURE = \
    lambda photo: SHUTTER_SPEED_FROM_LUMINANCE(LUMINANCE_FROM_PICTURE(photo), LUM_SPEED_PARAM_A, LUM_SPEED_PARAM_B)



class Camera:
    """
    Manages the pictures and graphical operations.
    """
    def __init__(self, configuration: Configuration):
        log("Initializing camera")
        
        # Populate the attributes with the 'image' data 
        if "image" not in vars(configuration).keys():
            log("WARNING! No image information present in the configuration! "
                "Please fix the error ASAP. Fallback values are being used.")
        
        for key, value in configuration.image.items():
            setattr(self, key, value)

        # Provide defaults for all the expected values of 'image'
        self.defaults = {
            "name": "no-name",
            "extension": "jpg",
            "time_format": "%H:%M",
            "date_format": "%d %B %Y",
            "width": 100,
            "height": 100,
            "ver_flip": False,
            "hor_flip": False,
            "rotation": 0,
            "jpeg_quality": 90,
            "jpeg_subsampling": 0,
            "background_color": (0,0,0,0),
            "calibrate": False,
        }

        # Check the calibration flag
        if os.path.exists(CALIBRATION_FLAG):
            with open(CALIBRATION_FLAG, 'r') as calib:
                try:
                    if "ON" in calib.read():
                        self.defaults["calibrate"] = True
                except Exception as e:
                    log_error("Something happened trying to read the calibration flag for the webcam. Ignoring it.", e)

        log(f"Calibration data collection is {'ON' if self.defaults['calibrate'] else 'OFF'}")

        # Check if the calibration parameters are overridden
        if os.path.exists(CALIBRATED_PARAMETERS):
            with open(CALIBRATED_PARAMETERS, 'r') as calib:
                try:
                    LUM_SPEED_PARAM_A, LUM_SPEED_PARAM_B = calib.readlines()[0].split(" ")
                    log(f"Camera configuration for low light: A={LUM_SPEED_PARAM_A}, B={LUM_SPEED_PARAM_B}")
                except Exception as e:
                    log_error("Something happened trying to read the overridden calibration parameters for the webcam. Ignoring them.", e)

        # There might be no overlays
        if "overlays" in vars(configuration).keys():
            self.overlays = configuration.overlays
        else:
            self.overlays = {}
        
        # Image name
        self.temp_photo_path = PATH / ('.temp_image.' + self.extension)
        self.processed_image_path = PATH / ('.final_image.' + self.extension)
        

    def __getattr__(self, name):
        """ 
        Provide some fallback value for all the expected fields of 'image'.
        Logs the access to highlight values that are not set, but were used.
        """
        value = self.defaults.get(name, None)
        #log(f"WARNING: Accessing default value for {name}: {value}")
        return value
        

    def take_picture(self) -> None:
        """
        Takes the picture and renders the elements on it.
        """
        # NOTE: let exceptions here escalate. Do not catch or at least rethrow!
        self.shoot_picture()
        self.process_picture()
            

    def shoot_picture(self) -> None:
        """
        Shoots the picture using PiCamera.
        If the luminance is found to be too low, adjusts the shutter speed camera value and tries again.
        """
        self._shoot_picture()
        
        # Test the luminance: if the picture is bright enough, return
        photo = Image.open(str(PATH / self.temp_photo_path))
        luminance = LUMINANCE_FROM_PICTURE(photo)
        if luminance >= MINIMUM_DAYLIGHT_LUMINANCE:
            return

        # We're in low light conditions.
        log(f"Low light detected: {luminance:.2f} (min is {MINIMUM_DAYLIGHT_LUMINANCE}).")

        if not self.calibrate:
            # Calculate new shutter speed and retry
            shutter_speed = SHUTTER_SPEED_FROM_PICTURE(photo)
            log(f"Shooting again with exposure time set to {shutter_speed/10**6:.2f}s. Expected final luminance: {TARGET_LUMINANCE(luminance):.2f}.")
            self._shoot_picture(shutter_speed=shutter_speed)

        else:
            # We're in low light conditions and we're recalibrating the camera.
            # Do a binary search over the shutter speed space, within 0.03s and 3s
            # Might require several attempts, but is bound to max 20 (see exit conditions)
            min_speed = MIN_SHUTTER_SPEED
            max_speed = MAX_SHUTTER_SPEED
            luminosity_margin = TARGET_LUMINOSITY_MARGIN
            target_luminance = TARGET_LUMINANCE(luminance)
            log(f"Entering calibration procedure. Parameters: min shutter speed = {MIN_SHUTTER_SPEED}, max shutter speed = {MAX_SHUTTER_SPEED}, target luminance = {target_luminance}")

            i = 0
            while True:
                
                # Update shutter speed and read resulting luminosity
                i += 1
                shutter_speed = int((min_speed + max_speed) / 2)
                self._shoot_picture(shutter_speed=shutter_speed)
                new_luminance = LUMINANCE_FROM_PICTURE(Image.open(str(PATH / self.temp_photo_path)))
                
                if new_luminance >= target_luminance - TARGET_LUMINOSITY_MARGIN and \
                   new_luminance <= target_luminance + TARGET_LUMINOSITY_MARGIN:
                    log(f"Tentative {i}: successful! Initial luminance: {luminance:.2f}, final luminance: {new_luminance:.2f}, shutter speed: {shutter_speed}")
                    
                    with open(CALIBRATION_CSV, 'a') as table:
                        table.write(f"{luminance:.2f}\t{new_luminance:.2f}\t{shutter_speed}\n")
                    break

                if new_luminance < target_luminance - TARGET_LUMINOSITY_MARGIN:
                    log(f"Tentative {i}: too dark. Luminance: {new_luminance:.2f}, speed: {shutter_speed/10**6:.2f}. Increasing!")
                    min_speed = shutter_speed

                else:
                    log(f"Tentative {i}: too bright. Luminance: {new_luminance:.2f}, speed: {shutter_speed/10**6:.2f}. Decreasing!")
                    max_speed = shutter_speed

                # Exit condition - 20 iterations with the default max/min speeds
                if min_speed + 5 > max_speed:
                    log(f"Search failed! Using the last value ({shutter_speed}, resulting luminance: {new_luminance}) and exiting the calibration procedure.")
                    break


    def _shoot_picture(self, shutter_speed: Optional[int] = None) -> None:
        """ 
        Actually shoots the picture using PiCamera.
        shutter_speed is useful for evening and night picture, and setting it triggers the evening mode.
        """
        log("Adjusting camera...")

        # The framerate sets a maximum to the exposure time.
        # Night pictures must set it explicitly or it will never exceed 30000 (which is way too fast)
        if shutter_speed:
            framerate = (10**6) / (shutter_speed)
        else:
            framerate = (10**6) / 30000  # default framerate
        
        with PiCamera(framerate=framerate) as camera:

            if int(self.width) > camera.MAX_RESOLUTION.width:
                log(f"WARNING! The requested image width ({self.width}) "
                    f"exceeds the maximum width resolution for this camera ({camera.MAX_RESOLUTION.width}). "
                    "Using the maximum width resolution instead.")
                self.width = camera.MAX_RESOLUTION.width

            if int(self.height) > camera.MAX_RESOLUTION.height:
                log(f"WARNING! The requested image height ({self.height}) "
                    f"exceeds the maximum height resolution for this camera ({camera.MAX_RESOLUTION.height}). "
                    "Using the maximum height resolution instead.")
                self.height = camera.MAX_RESOLUTION.height

            camera.resolution = (int(self.width), int(self.height))
            camera.vflip = self.ver_flip
            camera.hflip = self.hor_flip
            camera.rotation = int(self.rotation)
            camera.awb_mode = "sunlight"

            # Give the camera firmwaresome time to adjust
            if shutter_speed:
                camera.shutter_speed = shutter_speed
                camera.iso = 800
                sleep(4)
                camera.exposure_mode = "off"
                log("Taking low light picture")
            else:
                sleep(4)
                log("Taking picture")

            camera.capture(str(PATH / self.temp_photo_path))

            photo = Image.open(str(PATH / self.temp_photo_path))
            luminance = LUMINANCE_FROM_PICTURE(photo)
            log(f"Picture taken. Luminance: {luminance:.2f}.")


    def process_picture(self) -> None:
        """ 
        Renders text and images over the picture and saves the resulting image.
        """
        log("Processing picture")

        # Open and measures the picture
        try:
            photo = Image.open(self.temp_photo_path).convert("RGBA")
        except Exception as e:
            log_error("Failed to open the image for editing. "
                      "The photo will have no overlays applied.", e)
            return

        # Create the overlay images
        rendered_overlays = []
        for position, data in self.overlays.items():
            try:
                overlay = Overlay(position, data, photo.width, photo.height, self.date_format, self.time_format)
                if overlay.rendered_image:
                    rendered_overlays.append(overlay)
                    
            except Exception as e:
                log_error(f"Something happened processing the overlay {position}. "
                "This overlay will be skipped.", e)
        
        # Calculate final image size
        border_top = 0
        border_bottom = 0
        for overlay in rendered_overlays:                        
            # If this overlay is out of the picture, add its height to the 
            # final image size (above or below)
            if not overlay.over_the_picture:
                if overlay.vertical_position == "top":
                    border_top = max(border_top, overlay.rendered_image.height)
                else:
                    border_bottom = max(border_bottom, overlay.rendered_image.height)
        total_height = photo.height + border_top + border_bottom

        # Generate canvas of the correct size
        image = Image.new("RGBA", 
                          (photo.width, total_height),
                          color=self.background_color)

        # Add the picture on the canvas
        image.paste(photo, (0, border_top))

        # Add the overlays on the canvas in the right position
        for overlay in rendered_overlays:
            if overlay.rendered_image:  # it might be None if it failed along the way
                x, y = overlay.compute_position(image.width, image.height, border_top, border_bottom)
                image.paste(overlay.rendered_image, (x, y), mask=overlay.rendered_image)  # mask is to allow for transparent images
        
        # Save the image appropriately
        if self.extension.lower() in ["jpg", "jpeg"]:
            image = image.convert('RGB')
            image.save(PATH / self.processed_image_path, format='JPEG', 
                subsampling=self.jpeg_subsampling, quality=self.jpeg_quality)
        else:
            image.save(PATH / self.processed_image_path)
        
        


class Overlay:
    """
    Represents one overlay to add to the picture.
    """
    def __init__(self, position: str, data: Dict, photo_width: int, photo_height: int, date_format: Optional[str], time_format: Optional[str]):
        log(f"Creating overlay {position}")
        
        # Populate the attributes with the overlay data 
        for key, value in data.items():
            setattr(self, key, value)
        self.date_format = date_format if date_format else "%d %B %Y"
        self.time_format = time_format if time_format else "%H:%M"

        # Store position information
        try:
            self.vertical_position, self.horizontal_position = position.lower().split("_")
            if ((self.vertical_position != "top" and 
                 self.vertical_position != "bottom") or
                (self.horizontal_position != "left" and 
                 self.horizontal_position != "center" and 
                 self.horizontal_position != "right")):
                raise ValueError()
        except Exception as e:
            log("The position of this overlay ({position}) is malformed. "
                "It must be one of the following: top_left, top_center, "
                "top_right, bottom_left, bottom_center, bottom_right."
                "This overlay will be skipped", e)
            return
            
        # Find the type of overlay
        if not data.get("type", None) or not isinstance(data.get("type"), str):
            log(f"Overlay type not specified for position "
                f"{position}. This overlay will be skipped.")
            return
        self.type = data.get("type")

        self.rendered_image = None  # Where the rendered overlay is stored if can be generated
        self.defaults = {
            "font_size": 25,
            "padding_ratio": 0.2,
            "text": "~~~ DEFAULT TEXT ~~~",
            "font_color": (0, 0, 0),
            "background_color": (255, 255, 255, 0),
            "image": "fallback-pixel.png",
            "width": None,   # Might be unset to retain aspect ratio
            "heigth": None,  # Might be unset to retain aspect ratio
            "over_the_picture": False,
        }

        if self.type == "text":
            self.rendered_image = self.create_text_overlay(photo_width, photo_height)

        elif self.type == "image":
            self.rendered_image = self.create_image_overlay()
        
        else:
            log_error(f"Overlay name '{kind}' not recognized. Valid names: "
            "text, image. This overlay will be skipped.")
            return


    def __getattr__(self, name):
        """ 
        Provide some fallback value for all the expected fields of 'overlay'.
        Logs the access to highlight values that are not set, but were used.
        """
        value = self.defaults.get(name, None)
        #log(f"WARNING: Accessing default value for {name}: {value}")
        return value
        
    
    def compute_position(self, image_width: int, image_height: int, 
                border_top: int, border_bottom: int) -> Tuple[int, int]:
        """
        Returns the x,y position in the picture where this overlay 
        should be pasted.
        """
        x, y = 0, 0
        
        if self.horizontal_position == "left":
            x = 0
            
        elif self.horizontal_position == "right":
            x = image_width - self.rendered_image.width
            
        elif self.horizontal_position == "center":
            x = int((image_width - self.rendered_image.width)/2)

        if self.vertical_position == "top":
            if self.over_the_picture:
                y = border_top
            else:
                y = 0
                
        elif self.vertical_position == "bottom":
            if self.over_the_picture:
                y = image_height - self.rendered_image.height - border_bottom
            else:
                y = image_height - self.rendered_image.height
                
        return x, y


    def create_text_overlay(self, photo_width: int, photo_height: int) -> Any:
        """ 
        Prepares an overlay containing text.
        In case of issues, self.overlay_image will stay None.
        """
        try:
            # Creates the font and calculate the line height
            font_size = self.font_size
            font = ImageFont.truetype("/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf", font_size)
            line_height = font.getsize("a")[1] * 1.5

            # Calculate the padding as a percentage of the line height
            padding_ratio = self.padding_ratio
            padding = math.ceil(line_height*padding_ratio)

            # Replace %%TIME and %%DATE with respective values
            time_string = datetime.datetime.now().strftime(self.time_format)
            date_string = datetime.datetime.now().strftime(self.date_format)
            self.text = self.text.replace("%%TIME", time_string)
            self.text = self.text.replace("%%DATE", date_string)

            # Calculate the dimension of the text with the padding added
            text_width, text_height = self.process_text(font, photo_width)
            text_size = (text_width + padding*2, text_height + padding*2)

            # Creates the image
            label = Image.new("RGBA", text_size, color=self.background_color)
            draw = ImageDraw.Draw(label)
            draw.text((padding, padding, padding), self.text, self.font_color, font=font)

            # Store it
            return label

        except Exception as e:
            log_error("Something unexpected happened while generating text the overlay. "+
            "This overlay will be skipped.", e)
            return


    def process_text(self, font: Any, max_line_length: int) -> Tuple[int, int]:
        """ 
        Measures and insert returns into the text to make it fit into the image.
        """
        # Insert as many returns as needed to make the text fit.
        lines = []
        for line in self.text.split("\n"):
            if font.getsize(line)[0] <= max_line_length:
                lines.append(line)
            else:
                new_line = ""
                for word in line.split(" "):
                    if font.getsize(new_line + word)[0] <= max_line_length:
                        new_line = new_line + word + " "
                    else:
                        lines.append(new_line)
                        new_line = word + " "
                if new_line != "":
                    lines.append(new_line)
        self.text = '\n'.join(lines)

        # Create a temporary image to measure the text size
        temp = Image.new("RGBA", (1,1))
        temp_draw = ImageDraw.Draw(temp)
        return temp_draw.textsize(self.text, font)


    def create_image_overlay(self) -> Any:
        """ 
        Prepares an overlay containing an image.
        Might return None in case of issues. 
        """
        overlay_image_path = IMAGE_OVERLAYS_PATH / self.path

        try:
            image = Image.open(overlay_image_path).convert("RGBA")
        except Exception as e:
            log_error(f"Image '{overlay_image_path}' can't be found or is "
            "impossible to open. This overlay will be skipped.", e)
            return

        try:
            # Calculate new dimension, retaining aspect ratio if necessary
            if self.width and not self.height:
                aspect_ratio = image.width / image.height
                self.height = math.ceil(self.width / aspect_ratio)

            if not self.width and self.height:
                aspect_ratio = image.height / image.width
                self.width = math.ceil(self.height / aspect_ratio)

            # Do not resize if no size is given
            if self.width and self.height:
                image = image.resize((self.width, self.height))

            padding_width = math.ceil(image.width*self.padding_ratio)
            padding_height = math.ceil(image.height*self.padding_ratio)

            overlay_size = (image.width+padding_width*2, image.height+padding_height*2)
            overlay = Image.new("RGBA", overlay_size, color=self.background_color)
            overlay.paste(image, (padding_width, padding_height), mask=image)

            return overlay
            
        except Exception as e:
            log_error("Something unexpected happened while generating "
                      "the image overlay. This overlay will be skipped.", e)
            return


            
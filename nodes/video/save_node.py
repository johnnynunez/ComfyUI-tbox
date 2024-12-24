import os
import re
import cv2
import sys
import json
import torch
import datetime
import itertools
import subprocess
import folder_paths
import numpy as np
from string import Template
from pathlib import Path
from PIL import Image, ExifTags
from PIL.PngImagePlugin import PngInfo
from .ffmpeg import ffmpeg_path, gifski_path
from ..utils import tensor_to_bytes, tensor_to_shorts, requeue_workflow



def gen_format_widgets(video_format):
    for k in video_format:
        if k.endswith("_pass"):
            for i in range(len(video_format[k])):
                if isinstance(video_format[k][i], list):
                    item = [video_format[k][i]]
                    yield item
                    video_format[k][i] = item[0]
        else:
            if isinstance(video_format[k], list):
                item = [video_format[k]]
                yield item
                video_format[k] = item[0]
               
def get_format_widget_defaults(format_name):
    video_format_path = folder_paths.get_full_path("VHS_video_formats", format_name + ".json")
    with open(video_format_path, 'r') as stream:
        video_format = json.load(stream)
    results = {}
    for w in gen_format_widgets(video_format):
        if len(w[0]) > 2 and 'default' in w[0][2]:
            default = w[0][2]['default']
        else:
            if type(w[0][1]) is list:
                default = w[0][1][0]
            else:
                #NOTE: This doesn't respect max/min, but should be good enough as a fallback to a fallback to a fallback
                default = {"BOOLEAN": False, "INT": 0, "FLOAT": 0, "STRING": ""}[w[0][1]]
        results[w[0][0]] = default
    return results
 
def get_video_formats():
    formats = []
    for format_name in folder_paths.get_filename_list("VHS_video_formats"):
        format_name = format_name[:-5]
        formats.append("video/" + format_name)
    return formats

def gifski_process(args, video_format, file_path, env):
    frame_data = yield
    with subprocess.Popen(args + video_format['main_pass'] + ['-f', 'yuv4mpegpipe', '-'],
                          stderr=subprocess.PIPE, stdin=subprocess.PIPE,
                          stdout=subprocess.PIPE, env=env) as procff:
        with subprocess.Popen([gifski_path] + video_format['gifski_pass']
                              + ['-q', '-o', file_path, '-'], stderr=subprocess.PIPE,
                              stdin=procff.stdout, stdout=subprocess.PIPE,
                              env=env) as procgs:
            try:
                while frame_data is not None:
                    procff.stdin.write(frame_data)
                    frame_data = yield
                procff.stdin.flush()
                procff.stdin.close()
                resff = procff.stderr.read()
                resgs = procgs.stderr.read()
                outgs = procgs.stdout.read()
            except BrokenPipeError as e:
                procff.stdin.close()
                resff = procff.stderr.read()
                resgs = procgs.stderr.read()
                raise Exception("An error occurred while creating gifski output\n" \
                        + "Make sure you are using gifski --version >=1.32.0\nffmpeg: " \
                        + resff.decode("utf-8") + '\ngifski: ' + resgs.decode("utf-8"))
    if len(resff) > 0:
        print(resff.decode("utf-8"), end="", file=sys.stderr)
    if len(resgs) > 0:
        print(resgs.decode("utf-8"), end="", file=sys.stderr)
    #should always be empty as the quiet flag is passed
    if len(outgs) > 0:
        print(outgs.decode("utf-8"))

def ffmpeg_process(args, video_format, video_metadata, file_path, env):

    res = None
    frame_data = yield
    total_frames_output = 0
    if video_format.get('save_metadata', 'False') != 'False':
        os.makedirs(folder_paths.get_temp_directory(), exist_ok=True)
        metadata = json.dumps(video_metadata)
        metadata_path = os.path.join(folder_paths.get_temp_directory(), "metadata.txt")
        #metadata from file should  escape = ; # \ and newline
        metadata = metadata.replace("\\","\\\\")
        metadata = metadata.replace(";","\\;")
        metadata = metadata.replace("#","\\#")
        metadata = metadata.replace("=","\\=")
        metadata = metadata.replace("\n","\\\n")
        metadata = "comment=" + metadata
        with open(metadata_path, "w") as f:
            f.write(";FFMETADATA1\n")
            f.write(metadata)
        m_args = args[:1] + ["-i", metadata_path] + args[1:] + ["-metadata", "creation_time=now"]
        print(f'ffmpeg: {m_args}')
        with subprocess.Popen(m_args + [file_path], stderr=subprocess.PIPE,
                              stdin=subprocess.PIPE, env=env) as proc:
            try:
                while frame_data is not None:
                    proc.stdin.write(frame_data)
                    #TODO: skip flush for increased speed
                    frame_data = yield
                    total_frames_output+=1
                proc.stdin.flush()
                proc.stdin.close()
                res = proc.stderr.read()
            except BrokenPipeError as e:
                err = proc.stderr.read()
                #Check if output file exists. If it does, the re-execution
                #will also fail. This obscures the cause of the error
                #and seems to never occur concurrent to the metadata issue
                if os.path.exists(file_path):
                    raise Exception("An error occurred in the ffmpeg subprocess:\n" \
                            + err.decode("utf-8"))
                #Res was not set
                print(err.decode("utf-8"), end="", file=sys.stderr)
                print("An error occurred when saving with metadata")
    if res != b'':
        with subprocess.Popen(args + [file_path], stderr=subprocess.PIPE,
                              stdin=subprocess.PIPE, env=env) as proc:
            try:
                while frame_data is not None:
                    proc.stdin.write(frame_data)
                    frame_data = yield
                    total_frames_output+=1
                proc.stdin.flush()
                proc.stdin.close()
                res = proc.stderr.read()
            except BrokenPipeError as e:
                res = proc.stderr.read()
                raise Exception("An error occurred in the ffmpeg subprocess:\n" \
                        + res.decode("utf-8"))
    yield total_frames_output
    if len(res) > 0:
        print(res.decode("utf-8"), end="", file=sys.stderr)
        
def to_pingpong(inp):
    if not hasattr(inp, "__getitem__"):
        inp = list(inp)
    yield from inp
    for i in range(len(inp)-2,0,-1):
        yield inp[i]
        
def apply_format_widgets(format_name, kwargs):
    video_format_path = folder_paths.get_full_path("VHS_video_formats", format_name + ".json")
    with open(video_format_path, 'r') as stream:
        video_format = json.load(stream)

    for w in gen_format_widgets(video_format):
        assert(w[0][0] in kwargs)
        if len(w[0]) > 3:
            w[0] = Template(w[0][3]).substitute(val=kwargs[w[0][0]])
        else:
            w[0] = str(kwargs[w[0][0]])
    return video_format

class SaveVideoNode:
    @classmethod
    def INPUT_TYPES(s):
        ffmpeg_formats = get_video_formats()
        return {
            "required": {
                "path": ("STRING", {"multiline": True, "dynamicPrompts": False}),
                "format": (ffmpeg_formats,),
                "quality": ([100, 95, 90, 85, 80, 75, 70, 60, 50], {"default": 100}),
                "pingpong": ("BOOLEAN", {"default": False}),
            },
            "optional": {
                "images": ("IMAGE",),
                "audio": ("AUDIO",),
                "frame_rate": ("INT,FLOAT", { "default": 25.0, "step": 1.0, "min": 1.0, "max": 60.0 }),
                "meta_batch": ("BatchManager",),
            },
            "hidden": {
                "prompt": "PROMPT",
                "unique_id": "UNIQUE_ID"
            },
        }

    RETURN_TYPES = ()
    CATEGORY = "tbox/Video"
    FUNCTION = "save_video"
    OUTPUT_NODE = True
    
    def save_video(
        self,
        path,
        frame_rate=25,
        images=None,
        format="video/h264-mp4",
        quality=85,
        pingpong=False,
        audio=None,
        prompt=None,
        meta_batch=None,
        unique_id=None,
        manual_format_widgets=None,
    ):
        if images is None:
            return {}
        if isinstance(images, torch.Tensor) and images.size(0) == 0:
            return {}
        
        if frame_rate < 1:
            frame_rate = 1
        elif frame_rate > 120:
            frame_rate = 120
            
        num_frames = len(images)

        first_image = images[0]
        images = iter(images)
        
        file_path = os.path.abspath(path.split('\n')[0])
        output_dir = os.path.dirname(file_path)
        filename = os.path.basename(file_path)
        name, extension = os.path.splitext(filename)
       
        output_process = None

        video_metadata = {}
        if prompt is not None:
            video_metadata["prompt"] = prompt
            
        if meta_batch is not None and unique_id in meta_batch.outputs:
            (counter, output_process) = meta_batch.outputs[unique_id]
        else:
            counter = 0 
            output_process = None    

        format_type, format_ext = format.split("/")

        # Use ffmpeg to save a video
        if ffmpeg_path is None:
            raise ProcessLookupError(f"ffmpeg is required for video outputs and could not be found.\nIn order to use video outputs, you must either:\n- Install imageio-ffmpeg with pip,\n- Place a ffmpeg executable in {os.path.abspath('')}, or\n- Install ffmpeg and add it to the system path.")

        #Acquire additional format_widget values
        kwargs = None
        if manual_format_widgets is None:
            if prompt is not None:
                kwargs = prompt[unique_id]['inputs']
            else:
                manual_format_widgets = {}
        
        if kwargs is None:
            kwargs = get_format_widget_defaults(format_ext)
            missing = {}
            for k in kwargs.keys():
                if k in manual_format_widgets:
                    kwargs[k] = manual_format_widgets[k]
                else:
                    missing[k] = kwargs[k]
            if len(missing) > 0:
                print("Extra format values were not provided, the following defaults will be used: " + str(kwargs) + "\nThis is likely due to usage of ComfyUI-to-python. These values can be manually set by supplying a manual_format_widgets argument")
    
        video_format = apply_format_widgets(format_ext, kwargs)
        has_alpha = first_image.shape[-1] == 4
        dim_alignment = video_format.get("dim_alignment", 8)
        if (first_image.shape[1] % dim_alignment) or (first_image.shape[0] % dim_alignment):
            #output frames must be padded
            to_pad = (-first_image.shape[1] % dim_alignment,
                        -first_image.shape[0] % dim_alignment)
            padding = (to_pad[0]//2, to_pad[0] - to_pad[0]//2,
                        to_pad[1]//2, to_pad[1] - to_pad[1]//2)
            padfunc = torch.nn.ReplicationPad2d(padding)
            def pad(image):
                image = image.permute((2,0,1))#HWC to CHW
                padded = padfunc(image.to(dtype=torch.float32))
                return padded.permute((1,2,0))
            images = map(pad, images)
            new_dims = (-first_image.shape[1] % dim_alignment + first_image.shape[1],
                        -first_image.shape[0] % dim_alignment + first_image.shape[0])
            dimensions = f"{new_dims[0]}x{new_dims[1]}"
            print(f"Output images were not of valid resolution and have had padding applied: {dimensions}")
        else:
            dimensions = f"{first_image.shape[1]}x{first_image.shape[0]}"

        if pingpong:
            if meta_batch is not None:
                print("pingpong is incompatible with batched output")
            images = to_pingpong(images)
        
        images = map(tensor_to_bytes, images)
        if has_alpha:
            i_pix_fmt = 'rgba'
        else:
            i_pix_fmt = 'rgb24'
                
        args = [ffmpeg_path, "-v", "error", "-f", "rawvideo", "-pix_fmt", i_pix_fmt,
                "-s", dimensions, "-r", str(frame_rate), "-i", "-"]

        images = map(lambda x: x.tobytes(), images)
        env=os.environ.copy()
        if  "environment" in video_format:
            env.update(video_format["environment"])

        if "pre_pass" in video_format:
            images = [b''.join(images)]
            os.makedirs(folder_paths.get_temp_directory(), exist_ok=True)
            pre_pass_args = args[:13] + video_format['pre_pass']
            try:
                subprocess.run(pre_pass_args, input=images[0], env=env,
                                capture_output=True, check=True)
            except subprocess.CalledProcessError as e:
                raise Exception("An error occurred in the ffmpeg prepass:\n" \
                        + e.stderr.decode("utf-8"))
        if "inputs_main_pass" in video_format:
            args = args[:13] + video_format['inputs_main_pass'] + args[13:]

        if output_process is None:
            args += video_format['main_pass'] 
            output_process = ffmpeg_process(args, video_format, video_metadata, file_path, env)
            #Proceed to first yield
            output_process.send(None)
            if meta_batch is not None:
                meta_batch.outputs[unique_id] = (0, output_process)

        for image in images:
            output_process.send(image)
        if meta_batch is not None:
            requeue_workflow((meta_batch.unique_id, not meta_batch.has_closed_inputs))
        if meta_batch is None or meta_batch.has_closed_inputs:
            #Close pipe and wait for termination.
            try:
                total_frames_output = output_process.send(None)
                output_process.send(None)
            except StopIteration:
                pass
            if meta_batch is not None:
                meta_batch.outputs.pop(unique_id)
                #if len(meta_batch.outputs) == 0:
                #    meta_batch.reset()
        else:
            return {}

        a_waveform = None
        if audio is not None:
            try:
                #safely check if audio produced by VHS_LoadVideo actually exists
                a_waveform = audio['waveform']
            except:
                print(f'save audio >> not waveform')    
                pass
        if a_waveform is not None:
            # Create audio file if input was provided
            output_file_with_audio = f"{name}-audio{extension}"
            output_file_with_audio_path = os.path.join(output_dir, output_file_with_audio)
            if "audio_pass" not in video_format:
                print("Selected video format does not have explicit audio support")
                video_format["audio_pass"] = ["-c:a", "libopus"]


            # FFmpeg command with audio re-encoding
            #TODO: expose audio quality options if format widgets makes it in
            #Reconsider forcing apad/shortest
            channels = audio['waveform'].size(1)
            min_audio_dur = total_frames_output / frame_rate + 1
            mux_args = [ffmpeg_path, "-v", "error", "-i", file_path,
                        "-ar", str(audio['sample_rate']), "-ac", str(channels),
                        "-y","-f", "f32le", "-i", "-", "-c:v", "copy"] \
                        + video_format["audio_pass"] \
                        + ["-af", "apad=whole_dur="+str(min_audio_dur),
                            "-shortest", output_file_with_audio_path]

            audio_data = audio['waveform'].squeeze(0).transpose(0,1) \
                    .numpy().tobytes()
            try:
                res = subprocess.run(mux_args, input=audio_data,
                                        env=env, capture_output=True, check=True)
                if res.returncode == 0:
                    self.replace_file(output_file_with_audio_path, file_path)
            except subprocess.CalledProcessError as e:
                raise Exception("An error occured in the ffmpeg subprocess:\n" \
                        + e.stderr.decode("utf-8"))
            if res.stderr:
                print(res.stderr.decode("utf-8"), end="", file=sys.stderr)


        return {}
    
    @classmethod
    def VALIDATE_INPUTS(self, format, **kwargs):
        return True

    def replace_file(self, audio_path, file_path):
        try:
            # 删除 file_path 文件（如果存在）
            if os.path.exists(file_path):
                os.remove(file_path)
                print(f"Deleted file: {file_path}")
            else:
                print(f"File not found, skipping deletion: {file_path}")
            
            # 将 output_file_with_audio_path 重命名为 file_path
            os.rename(audio_path, file_path)
            print(f"Renamed {audio_path} to {file_path}")
        except Exception as e:
            print(f"An error occurred: {e}")
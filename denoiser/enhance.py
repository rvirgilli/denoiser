# Copyright (c) Facebook, Inc. and its affiliates.
# All rights reserved.
#
# This source code is licensed under the license found in the
# LICENSE file in the root directory of this source tree.
# author: adiyoss
from pathlib import Path
import argparse
import json
import logging
import os
import sys
from tqdm import tqdm

import torch
import torchaudio

from audio import Audioset, find_audio_files
import distrib, pretrained
from demucs import DemucsStreamer

from utils import LogProgress

logger = logging.getLogger(__name__)


def add_flags(parser):
    """
    Add the flags for the argument parser that are related to model loading and evaluation"
    """
    pretrained.add_model_flags(parser)
    parser.add_argument('--device', default="cpu")
    parser.add_argument('--dry', type=float, default=0,
                        help='dry/wet knob coefficient. 0 is only input signal, 1 only denoised.')
    parser.add_argument('--sample_rate', default=16_000, type=int, help='sample rate')
    parser.add_argument('--num_workers', type=int, default=10)
    parser.add_argument('--streaming', action="store_true",
                        help="true streaming evaluation for Demucs")


parser = argparse.ArgumentParser(
        'denoiser.enhance',
        description="Speech enhancement using Demucs - Generate enhanced files")
add_flags(parser)
parser.add_argument("--out_dir", type=str, default="enhanced",
                    help="directory putting enhanced wav files")
parser.add_argument("--batch_size", default=1, type=int, help="batch size")
parser.add_argument('-v', '--verbose', action='store_const', const=logging.DEBUG,
                    default=logging.INFO, help="more loggging")

group = parser.add_mutually_exclusive_group()
group.add_argument("--noisy_dir", type=str, default=None,
                   help="directory including noisy wav files")
group.add_argument("--noisy_json", type=str, default=None,
                   help="json file including noisy wav files")

parser.add_argument("--base_path", type=str, default=None,
                   help="json file including noisy wav files")


def get_estimate(model, noisy, args):
    torch.set_num_threads(1)
    if args.streaming:
        streamer = DemucsStreamer(model, dry=args.dry)
        with torch.no_grad():
            estimate = torch.cat([
                streamer.feed(noisy[0]),
                streamer.flush()], dim=1)[None]
    else:
        with torch.no_grad():
            estimate = model(noisy)
            estimate = (1 - args.dry) * estimate + args.dry * noisy
    return estimate


def save_wavs(estimates, noisy_sigs, filenames, out_dir, sr=16_000):
    # Write result
    for estimate, noisy, filename in zip(estimates, noisy_sigs, filenames):
        cls = filename.split('/')[-3]
        vid = filename.split('/')[-2]
        file = filename.split('/')[-1]

        if not os.path.exists(os.path.join(out_dir, cls)):
            os.makedirs(os.path.join(out_dir, cls))

        if not os.path.exists(os.path.join(out_dir, cls, vid)):
            os.makedirs(os.path.join(out_dir, cls, vid))

        filename = os.path.join(out_dir, cls, vid, file)

        #write(noisy, filename + "_noisy.wav", sr=sr)
        write(estimate, filename, sr=sr)


def write(wav, filename, sr=16_000):
    # Normalize audio if it prevents clipping
    wav = wav / max(wav.abs().max().item(), 1)
    torchaudio.save(filename, wav.cpu(), sr)


def get_dataset(args):
    if hasattr(args, 'dset'):
        paths = args.dset
    else:
        paths = args
    if paths.noisy_json:
        if not os.path.exists(paths.noisy_json):
            generate_json(args.base_path, paths.noisy_json)
        with open(paths.noisy_json) as f:
            files = json.load(f)
    elif paths.noisy_dir:
        files = find_audio_files(paths.noisy_dir)
    else:
        logger.warning(
            "Small sample set was not provided by either noisy_dir or noisy_json. "
            "Skipping enhancement.")
        return None
    return Audioset(files, with_path=True, sample_rate=args.sample_rate)


def enhance(args, model=None, local_out_dir=None):
    # Load model
    if not model:
        model = pretrained.get_model(args).to(args.device)
    model.eval()
    if local_out_dir:
        out_dir = local_out_dir
    else:
        out_dir = args.out_dir

    dset = get_dataset(args)
    if dset is None:
        return
    loader = distrib.loader(dset, batch_size=1)

    if distrib.rank == 0:
        os.makedirs(out_dir, exist_ok=True)
    distrib.barrier()

    with torch.no_grad():
        iterator = LogProgress(logger, loader, name="Generate enhanced files")
        for data in tqdm(iterator):
            # Get batch data
            noisy_signals, filenames = data
            noisy_signals = noisy_signals.to(args.device)
            # Forward
            estimate = get_estimate(model, noisy_signals, args)
            save_wavs(estimate, noisy_signals, filenames, out_dir, sr=args.sample_rate)

def generate_json(base_path, json_name, exts=[".wav"]):
    print('generation json file')
    audio_files = []
    for root, folders, files in os.walk(base_path, followlinks=True):
        for file in files:
            file = Path(root) / file
            if file.suffix.lower() in exts:
                audio_files.append(str(file.resolve()))
    meta = []
    for idx, file in enumerate(audio_files):
        siginfo, _ = torchaudio.info(file)
        length = siginfo.length // siginfo.channels
        meta.append((file, length))
    with open(json_name, 'w') as outfile:
        json.dump(meta, outfile)

if __name__ == "__main__":
    args = parser.parse_args()
    logging.basicConfig(stream=sys.stderr, level=args.verbose)
    logger.debug(args)
    enhance(args, local_out_dir=args.out_dir)

import os
import re
import sys
import traceback


def create_path(args, folder_path):
    try:
        pattern = re.compile(r'[\w]+=[\w.]+')
        to_add = ','.join(pattern.findall(args.__str__())) + '/'
        args.folder_path = folder_path + to_add
        os.makedirs(args.folder_path, exist_ok=True)
        # Do this purposely so that we don't forget!
        return args.folder_path
    except Exception:
        traceback.print_exc()
        sys.exit(3)
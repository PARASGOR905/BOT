from datetime import datetime, timedelta
import os
import logging
import random


log = logging.getLogger("NF-ESN")

def chrome_esn_generator():
    esn_file = '.esn'
    
    def gen_file():
        ESN_GEN = "".join(random.choice("0123456789ABCDEF") for _ in range(30))
        with open(esn_file, 'w') as file:
            file.write(f'NFCDCH-02-{ESN_GEN}')
    
    if not os.path.isfile(esn_file):
        log.warning("Generating a new Chrome ESN")
        gen_file()
    
    with open(esn_file, 'r') as f:
        esn = f.read().strip()

    # If it's a custom ESN (long enough or doesn't match the Chrome prefix logic), skip rotation/regeneration
    if not esn.startswith(('NFCDCH-02-', 'NFCDCH-01-', 'NFCDCH-')) or len(esn) > 45:
        return esn

    file_datetime = datetime.fromtimestamp(os.path.getmtime(esn_file))
    time_diff = datetime.now() - file_datetime
    if time_diff > timedelta(hours=6):
        log.warning("Old Chrome ESN detected, Generating a new one")
        gen_file()
        with open(esn_file, 'r') as f:
            esn = f.read().strip()

    return esn


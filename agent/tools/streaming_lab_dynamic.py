"""Host control for the fixed local dynamic-feed worker; never loads .env."""
import argparse
from streaming_lab_ops import command
from streaming_lab_workload import resources


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    parser.add_argument('command',choices=('start','pause','resume','status'))
    args=parser.parse_args()
    if args.command in ('start','resume'):
        sample=resources()
        if sample['windows_free_gib']<2 or sample['d_free_gib']<50:
            raise RuntimeError('Host resource budget insufficient')
    result=command(['docker','exec','newsagent-streaming-lab-dynamic-feed-1','python','-m',
                    'simulation.streaming_lab.dynamic_feed',args.command],timeout=25)
    print(result.decode('utf-8'))


if __name__=='__main__': main()

def format_progress(stage, filename, done, total, speed):
    fraction = min(1, max(0, done / total)) if total else 0
    filled = int(fraction * 10)
    bar = '█' * filled + '░' * (10 - filled)
    eta = f'{max(0, total-done)/speed/60:.1f} min' if speed else 'waiting for data'
    return (f'{stage}\n{filename}\n{bar} {fraction:.0%}\n'
            f'{done:,} / {total:,} bytes ({done/1_000_000:.1f} / {total/1_000_000:.1f} MB)\n'
            f'Speed: {speed/1_000_000:.2f} MB/s · ETA: {eta}')

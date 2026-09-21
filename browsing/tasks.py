from celery import shared_task


@shared_task(name='browsing.sweep_browser_sessions', ignore_result=True)
def sweep_browser_sessions():
    from browsing.sessions import sweep

    return sweep()

from celery import shared_task


@shared_task(name='missions.sweep_missions', ignore_result=True)
def sweep_missions():
    from missions.sweep import run_mission_sweep

    return run_mission_sweep()

from celery import shared_task


@shared_task(name='workspaces.sweep_workspaces', ignore_result=True)
def sweep_workspaces():
    from workspaces.sweep import run_workspace_sweep

    return run_workspace_sweep()

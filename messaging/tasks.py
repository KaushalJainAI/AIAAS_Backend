from celery import shared_task


@shared_task(name='messaging.purge_inbound', ignore_result=True)
def purge_inbound():
    from messaging.retention import purge_expired

    return purge_expired()

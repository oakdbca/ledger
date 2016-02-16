#from django.contrib.auth.models import User
from rollcall.models import EmailUser


# Custom pipeline to retrieve the user by the email in the details and log it
# in. The code checks for a verification_code parameter in order to validate
# that this is a password-less auth attempt, that way other social
# authentication will keep working.

def user_by_email(backend, details, *args, **kwargs):
    request_data = backend.strategy.request_data()
    if request_data.get('verification_code') and details.get('email'):
        try:
            user = EmailUser.objects.get(email=details['email'])
        except EmailUser.DoesNotExist:
            user = None
        return {'user': user}

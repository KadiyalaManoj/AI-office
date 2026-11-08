from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import jwt
from django.contrib.auth.models import User

@csrf_exempt
def process_payment(request):
    token = request.headers.get('Authorization').split()[1]
    payload = jwt.decode(token, 'secret_key', algorithms=['HS256'])
    user = User.objects.filter(username=payload['username']).first()
    if user:
        # Simulate payment processing
        return JsonResponse({'message': 'Payment processed successfully'}, status=200)
    return JsonResponse({'error': 'Invalid token'}, status=401)

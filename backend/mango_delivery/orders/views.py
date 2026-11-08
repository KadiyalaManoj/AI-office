from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import jwt
from django.contrib.auth.models import User

@csrf_exempt
def place_order(request):
    token = request.headers.get('Authorization').split()[1]
    payload = jwt.decode(token, 'secret_key', algorithms=['HS256'])
    user = User.objects.filter(username=payload['username']).first()
    if user:
        # Simulate order processing
        order_id = 'ORD12345'
        return JsonResponse({'order_id': order_id, 'message': 'Order placed successfully'}, status=200)
    return JsonResponse({'error': 'Invalid token'}, status=401)

@csrf_exempt
def track_order(request, order_id):
    # Simulate order tracking
    return JsonResponse({'order_id': order_id, 'status': 'Shipped'}, status=200)

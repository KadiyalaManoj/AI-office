from django.http import JsonResponse
from django.views.decorators.csrf import csrf_exempt
import jwt
from django.contrib.auth.models import User

@csrf_exempt
def get_inventory(request):
    # Simulate fetching inventory
    inventory = {
        'mango1': {'quantity': 100, 'price': 1.50},
        'mango2': {'quantity': 50, 'price': 2.00}
    }
    return JsonResponse(inventory, status=200)

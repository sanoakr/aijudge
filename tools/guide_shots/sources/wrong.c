#include <stdio.h>
int main(void){int n,x,mx=-1,mn=1<<30;scanf("%d",&n);for(int i=0;i<n;i++){scanf("%d",&x);if(x>mx)mx=x;if(x<mn)mn=x;}printf("%d %d\n",mx,mn);return 0;}
